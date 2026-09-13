//! Isolated integrity-verification profile, not a host ingestion or recall benchmark.
use memorycore_ai_broker::{
    Session,
    native::{
        Result,
        database::Database,
        knowledge::{Knowledge, binding},
    },
};
use rusqlite::Connection;
use serde_json::json;
use std::{fs, path::PathBuf, time::Instant};

fn main() -> Result<()> {
    let root = PathBuf::from(
        std::env::args()
            .nth(1)
            .ok_or("exclusive output directory required")?,
    );
    fs::create_dir(&root)?;
    let conn = Connection::open(root.join("capacity.sqlite3"))?;
    Database::unlock(&conn, Some("MEMORYCORE_AI_CAPACITY_TEST_KEY"))?;
    let db = Database::initialize(conn)?;
    db.conn.execute_batch(
        "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA secure_delete=ON;",
    )?;
    Knowledge::initialize(&db)?;
    let session: Session =
        serde_json::from_value(json!({"id":"verify-scale","scope":"synthetic:verify-scale",
        "source_root":root,"allow_admin":true}))?;
    let k = Knowledge::new(&db, &session)?;
    let raw = b"Synthetic inspection records require a second review before release.\n";
    fs::write(root.join("source.md"), raw)?;
    let source = binding("source.md", raw);
    let maximum = std::env::var("MEMORYCORE_AI_CAPACITY_MAX")
        .unwrap_or_else(|_| "100000".into())
        .parse::<usize>()?;
    let started = Instant::now();
    let mut count = 0;
    for target in [1_000, 10_000, 100_000]
        .into_iter()
        .filter(|n| *n <= maximum)
    {
        let seed = Instant::now();
        // Fixture setup uses larger transactions without inference or jobs.
        // Only the verification path below is under measurement.
        while count < target {
            db.conn.execute_batch("BEGIN IMMEDIATE")?;
            for _ in 0..512.min(target - count) {
                let saved = db.remember(&session.scope, &json!({"type":"semantic",
                    "subject":format!("Synthetic inspection R{count:06}"),
                    "summary":"Synthetic inspection records require a second review before release.",
                    "source":"source.md","source_hash":source["sha256"]}))?;
                k.put("source", source.clone(), saved["memory_id"].as_str(), None)?;
                count += 1;
            }
            db.conn.execute_batch("COMMIT")?;
        }
        let seed_seconds = seed.elapsed().as_secs_f64();
        db.conn
            .execute_batch("PRAGMA wal_checkpoint(TRUNCATE); BEGIN")?;
        let verify = Instant::now();
        db.verify_scope(&session.scope)?;
        let core_seconds = verify.elapsed().as_secs_f64();
        k.verify_items()?;
        let total_seconds = verify.elapsed().as_secs_f64();
        db.conn.execute_batch("ROLLBACK")?;
        println!(
            "{}",
            json!({"records":target,"encrypted":db.encrypted(),"verified":true,
            "seed_seconds":seed_seconds,"core_seconds":core_seconds,
            "knowledge_seconds":total_seconds-core_seconds,"verify_seconds":total_seconds,
            "elapsed_seconds":started.elapsed().as_secs_f64(),"within_broker_deadline":total_seconds<10.0,
            "limitations":["Direct synchronous verification, without a broker deadline or competing traffic.",
                "Synthetic setup bypasses source-file ingestion and vector indexing; not a throughput claim."]})
        );
    }
    Ok(())
}
