//! Coalesced WAL maintenance outside the authoritative writer response path.
use super::{Result, database::Database, ensure};
use serde_json::{Value, json};
use std::sync::{Arc, Mutex, mpsc};

const WAL_RESTART_BYTES: i64 = 64 * 1024 * 1024;
const WAL_RETAIN_BYTES: i64 = 128 * 1024 * 1024;

pub struct Checkpoints {
    sender: Option<mpsc::SyncSender<()>>,
    result: Arc<Mutex<Option<Value>>>,
    worker: Option<std::thread::JoinHandle<()>>,
}

pub fn configure_writer(conn: &rusqlite::Connection) -> Result<()> {
    conn.execute_batch("PRAGMA synchronous=FULL; PRAGMA wal_autocheckpoint=0;")?;
    // Reuse allocated WAL pages across ordinary restarts instead of repeatedly
    // truncating below the restart threshold and extending on durable writes.
    conn.pragma_update(None, "journal_size_limit", WAL_RETAIN_BYTES)?;
    Ok(())
}

fn observe(conn: &rusqlite::Connection, restart: bool) -> Value {
    let started = std::time::Instant::now();
    let sql = if restart {
        "PRAGMA wal_checkpoint(RESTART)"
    } else {
        "PRAGMA wal_checkpoint(PASSIVE)"
    };
    let mut result = match conn.query_row(sql, [], |r| {
        Ok((
            r.get::<_, i64>(0)?,
            r.get::<_, i64>(1)?,
            r.get::<_, i64>(2)?,
        ))
    }) {
        Ok((busy, log, copied)) => {
            json!({"busy":busy,"log_pages":log,"checkpointed_pages":copied,"failed":false})
        }
        Err(_) => json!({"failed":true}),
    };
    result["ms"] = json!(started.elapsed().as_secs_f64() * 1000.0);
    result["background"] = json!(true);
    result
}

fn page_bytes(conn: &rusqlite::Connection) -> Result<i64> {
    let value: rusqlite::types::Value = conn.query_row("PRAGMA page_size", [], |row| row.get(0))?;
    let bytes = match value {
        rusqlite::types::Value::Integer(bytes) => bytes,
        // SQLCipher exposes this numeric setting through its text pragma adapter.
        rusqlite::types::Value::Text(bytes) => bytes.parse::<i64>()?,
        _ => return Err("invalid database page size".into()),
    };
    ensure(
        (512..=65536).contains(&bytes) && (bytes as u64).is_power_of_two(),
        "invalid database page size",
    )?;
    Ok(bytes)
}

impl Checkpoints {
    pub fn start(config: &crate::Config) -> Result<Self> {
        let db = Database::open_keyed(
            std::path::Path::new(&config.database),
            false,
            config.key_env.as_deref(),
        )?;
        configure_writer(&db.conn)?;
        // Bound restart lock waits; storage I/O itself is not a hard wall-time quota.
        db.conn.busy_timeout(std::time::Duration::from_millis(10))?;
        let restart_pages = WAL_RESTART_BYTES / page_bytes(&db.conn)?;
        let mut last_restart: Option<std::time::Instant> = None;
        Ok(Self::spawn(move || {
            let mut result = observe(&db.conn, false);
            if result["log_pages"]
                .as_i64()
                .is_some_and(|pages| pages >= restart_pages)
                && result["checkpointed_pages"] == result["log_pages"]
                && last_restart.is_none_or(|attempt| attempt.elapsed().as_secs() >= 5)
            {
                last_restart = Some(std::time::Instant::now());
                result["restart"] = observe(&db.conn, true);
            }
            result
        }))
    }

    fn spawn(mut work: impl FnMut() -> Value + Send + 'static) -> Self {
        let (sender, receiver) = mpsc::sync_channel(1);
        let result = Arc::new(Mutex::new(None));
        let completed = result.clone();
        let worker = std::thread::spawn(move || {
            let mut sequence = 0;
            while receiver.recv().is_ok() {
                let mut receipt = work();
                sequence += 1;
                receipt["sequence"] = json!(sequence);
                if let Ok(mut target) = completed.lock() {
                    *target = Some(receipt);
                }
            }
        });
        Self {
            sender: Some(sender),
            result,
            worker: Some(worker),
        }
    }

    pub fn request(&self) -> bool {
        self.sender
            .as_ref()
            .is_some_and(|sender| sender.try_send(()).is_ok())
    }

    pub fn take(&self) -> Option<Value> {
        self.result.lock().ok().and_then(|mut result| result.take())
    }
}

impl Drop for Checkpoints {
    fn drop(&mut self) {
        self.sender.take();
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    #[test]
    fn full_durability_is_retained_when_automatic_checkpointing_is_disabled() {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        configure_writer(&conn).unwrap();
        assert_eq!(page_bytes(&conn).unwrap(), 4096);
        assert_eq!(
            conn.query_row("PRAGMA synchronous", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            2
        );
        assert_eq!(
            conn.query_row("PRAGMA wal_autocheckpoint", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert_eq!(
            conn.query_row("PRAGMA journal_size_limit", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            WAL_RETAIN_BYTES
        );
    }

    #[cfg(feature = "sqlcipher")]
    #[test]
    fn encrypted_page_size_accepts_the_cipher_pragma_text_type() {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        conn.execute_batch("PRAGMA cipher_log_level=ERROR; PRAGMA key='synthetic checkpoint fixture'; CREATE TABLE fixture(value INTEGER);").unwrap();
        let raw: rusqlite::types::Value = conn
            .query_row("PRAGMA page_size", [], |r| r.get(0))
            .unwrap();
        assert!(matches!(raw, rusqlite::types::Value::Text(_)));
        assert_eq!(page_bytes(&conn).unwrap(), 4096);
    }

    #[test]
    fn restart_respects_old_readers_then_reuses_wal_without_losing_writes() {
        let root = std::env::temp_dir().join(format!("memorycore-ai-wal-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&root).unwrap();
        let path = root.join("fixture.sqlite3");
        {
            let writer = rusqlite::Connection::open(&path).unwrap();
            writer
                .execute_batch("PRAGMA journal_mode=WAL; CREATE TABLE fixture(value BLOB);")
                .unwrap();
            configure_writer(&writer).unwrap();
            writer
                .execute("INSERT INTO fixture VALUES(zeroblob(20971520))", [])
                .unwrap();
            let reader = rusqlite::Connection::open(&path).unwrap();
            reader
                .execute_batch("BEGIN; SELECT * FROM fixture;")
                .unwrap();
            writer
                .execute("INSERT INTO fixture VALUES(zeroblob(100))", [])
                .unwrap();
            let allocated = std::fs::metadata(root.join("fixture.sqlite3-wal"))
                .unwrap()
                .len();
            let maintenance = rusqlite::Connection::open(&path).unwrap();
            maintenance
                .busy_timeout(std::time::Duration::from_millis(10))
                .unwrap();
            assert_eq!(observe(&maintenance, true)["busy"], 1);
            assert_eq!(
                reader
                    .query_row("SELECT count(*) FROM fixture", [], |r| r.get::<_, i64>(0))
                    .unwrap(),
                1
            );
            reader.execute_batch("ROLLBACK").unwrap();
            assert_eq!(observe(&maintenance, true)["busy"], 0);
            writer
                .execute("INSERT INTO fixture VALUES(zeroblob(100))", [])
                .unwrap();
            assert_eq!(
                writer
                    .query_row("SELECT count(*) FROM fixture", [], |r| r.get::<_, i64>(0))
                    .unwrap(),
                3
            );
            assert_eq!(
                std::fs::metadata(root.join("fixture.sqlite3-wal"))
                    .unwrap()
                    .len(),
                allocated
            );
            // Above the configured retention target, reset still releases space.
            writer
                .pragma_update(None, "journal_size_limit", 16 * 1024 * 1024)
                .unwrap();
            assert_eq!(observe(&maintenance, true)["busy"], 0);
            writer
                .execute("INSERT INTO fixture VALUES(zeroblob(100))", [])
                .unwrap();
            assert!(
                std::fs::metadata(root.join("fixture.sqlite3-wal"))
                    .unwrap()
                    .len()
                    <= 16 * 1024 * 1024
            );
            assert_eq!(
                writer
                    .query_row("PRAGMA integrity_check", [], |r| r.get::<_, String>(0))
                    .unwrap(),
                "ok"
            );
        }
        std::fs::remove_file(path).unwrap();
        std::fs::remove_dir(root).unwrap();
    }

    #[test]
    fn slow_checkpoint_is_coalesced_and_shutdown_waits_for_owned_work() {
        let count = Arc::new(AtomicUsize::new(0));
        let calls = count.clone();
        let (started, entered) = mpsc::channel();
        let (release, wait) = mpsc::channel();
        let service = Checkpoints::spawn(move || {
            if calls.fetch_add(1, Ordering::SeqCst) == 0 {
                started.send(()).unwrap();
                wait.recv().unwrap();
            }
            json!({"failed":false})
        });
        assert!(service.request());
        entered
            .recv_timeout(std::time::Duration::from_secs(2))
            .unwrap();
        assert_eq!((0..100).filter(|_| service.request()).count(), 1);
        assert!(service.take().is_none());
        release.send(()).unwrap();
        drop(service);
        assert_eq!(count.load(Ordering::SeqCst), 2);
    }
}
