//! Host-only encrypted snapshot creation; never replaces or activates a vault.
use super::{Result, database::Database, ensure, json};
use crate::Config;
use rusqlite::{
    Connection,
    backup::{Backup, StepResult},
};
use serde::Deserialize;
use serde_json::json as j;
use std::{
    io::Read,
    path::Path,
    time::{Duration, Instant},
};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Options {
    destination: String,
    key_env: String,
}

pub fn command() -> Result<()> {
    let args: Vec<_> = std::env::args().collect();
    ensure(
        args.len() == 4 && args[2] == "--config",
        "configuration required",
    )?;
    let mut raw = Vec::new();
    std::fs::File::open(&args[3])?
        .take((crate::MAX_FRAME + 1) as u64)
        .read_to_end(&mut raw)?;
    ensure(raw.len() <= crate::MAX_FRAME, "configuration too large")?;
    let config: Config = serde_json::from_value(json(std::str::from_utf8(&raw)?)?)?;
    config.validate()?;
    ensure(
        config.backend == "native" && config.key_env.is_some(),
        "encrypted native source required",
    )?;
    raw.clear();
    std::io::stdin()
        .take((crate::MAX_FRAME + 1) as u64)
        .read_to_end(&mut raw)?;
    ensure(raw.len() <= crate::MAX_FRAME, "backup options too large")?;
    let options: Options = serde_json::from_value(json(std::str::from_utf8(&raw)?)?)?;
    ensure(
        Path::new(&options.destination).is_absolute(),
        "absolute destination required",
    )?;
    ensure(
        !options.key_env.is_empty()
            && options
                .key_env
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_'),
        "invalid destination key source",
    )?;
    let source_key = zeroize::Zeroizing::new(std::env::var(config.key_env.as_ref().unwrap())?);
    let destination_key = zeroize::Zeroizing::new(std::env::var(&options.key_env)?);
    ensure(
        !source_key.eq_ignore_ascii_case(&destination_key),
        "independent destination key required",
    )?;
    let db = Database::open_keyed(Path::new(&config.database), true, config.key_env.as_deref())?;
    ensure(db.encrypted(), "source encryption required")?;
    // A pinned read snapshot includes committed WAL data without blocking WAL writers.
    db.conn.execute_batch("BEGIN")?;
    let revision: i64 = db
        .conn
        .query_row("SELECT revision FROM vault_state", [], |r| r.get(0))?;
    let file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&options.destination)?;
    drop(file);
    let result = (|| -> Result<(usize, usize)> {
        let mut destination = Connection::open(&options.destination)?;
        Database::unlock(&destination, Some(&options.key_env))?;
        destination.execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;")?;
        {
            let backup = Backup::new(&db.conn, &mut destination)?;
            let started = Instant::now();
            loop {
                ensure(
                    started.elapsed() < Duration::from_secs(30),
                    "backup deadline exceeded",
                )?;
                match backup.step(128)? {
                    StepResult::Done => break,
                    StepResult::More => (),
                    _ => std::thread::sleep(Duration::from_millis(10)),
                }
            }
        }
        // A restored snapshot cannot know which chats were deleted after capture.
        // Preserve facts and tombstones, but require a newer host lifecycle receipt.
        let has_chats: bool = destination.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='native_chat')",
            [],
            |r| r.get(0),
        )?;
        let reconciliation_required = if has_chats {
            destination.execute(
                "UPDATE native_chat SET state='unknown' WHERE state!='deleted'",
                [],
            )?
        } else {
            0
        };
        let has_projects: bool = destination.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='native_project')",
            [],
            |r| r.get(0),
        )?;
        let projects_requiring_reconciliation = if has_projects {
            destination.execute(
                "UPDATE native_project SET state='unknown' WHERE state!='deleted'",
                [],
            )?
        } else {
            0
        };
        drop(destination);
        let restored = Database::open_keyed(
            Path::new(&options.destination),
            true,
            Some(&options.key_env),
        )?;
        ensure(restored.encrypted(), "destination encryption unavailable")?;
        let integrity: String = restored
            .conn
            .query_row("PRAGMA integrity_check", [], |r| r.get(0))?;
        ensure(integrity == "ok", "snapshot integrity failure")?;
        ensure(
            !restored
                .conn
                .prepare("PRAGMA foreign_key_check")?
                .query([])?
                .next()?
                .is_some(),
            "snapshot foreign key failure",
        )?;
        ensure(
            !restored
                .conn
                .prepare("PRAGMA cipher_integrity_check")?
                .query([])?
                .next()?
                .is_some(),
            "snapshot cipher integrity failure",
        )?;
        let copied: i64 = restored
            .conn
            .query_row("SELECT revision FROM vault_state", [], |r| r.get(0))?;
        ensure(copied == revision, "snapshot revision mismatch")?;
        drop(restored);
        std::fs::OpenOptions::new()
            .write(true)
            .open(&options.destination)?
            .sync_all()?;
        Ok((reconciliation_required, projects_requiring_reconciliation))
    })();
    let (reconciliation_required, projects_requiring_reconciliation) = match result {
        Ok(count) => count,
        Err(error) => {
            // Only the new destination created by this invocation is removed.
            let _ = std::fs::remove_file(&options.destination);
            return Err(error);
        }
    };
    println!(
        "{}",
        j!({"backup_created":true,"encrypted":true,"revision":revision,"activated":false,
            "chats_requiring_reconciliation":reconciliation_required,
            "projects_requiring_reconciliation":projects_requiring_reconciliation})
    );
    Ok(())
}
