//! Source-bound SQLCipher capacity and quantisation oracle, not an inference SLA.
use memorycore_ai_broker::{
    Session,
    native::{self, Result, admin, database::Database, knowledge::Knowledge, vectors},
};
use rusqlite::Connection;
use serde_json::json;
use std::{fs, path::PathBuf, time::Instant};

const DIM: usize = 384;
fn vector(mut seed: u64) -> Vec<f32> {
    let mut v = Vec::with_capacity(DIM);
    for _ in 0..DIM {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        v.push((seed as u32) as f32 / u32::MAX as f32 - 0.5);
    }
    let norm = v.iter().map(|x| x * x).sum::<f32>().sqrt();
    v.iter_mut().for_each(|x| *x /= norm);
    v
}
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
    let session: Session = serde_json::from_value(
        json!({"id":"capacity","scope":"synthetic:capacity","source_root":root,
        "allow_admin":true}),
    )?;
    let k = Knowledge::new(&db, &session)?;
    vectors::execute(
        &k,
        "vector-configure",
        &json!({"model":"synthetic-scale-v1","dimensions":DIM}),
    )?;
    let mut oracle = turbovec::IdMapIndex::new(DIM, 4)?;
    let mut ids = Vec::new();
    let mut all = Vec::new();
    let started = Instant::now();
    let maximum = std::env::var("MEMORYCORE_AI_CAPACITY_MAX")
        .unwrap_or_else(|_| "100000".into())
        .parse::<usize>()?;
    let plan = db
        .conn
        .prepare(&format!("EXPLAIN QUERY PLAN {}", vectors::INDEX_ROWS_SQL))?
        .query_map(rusqlite::params![session.scope, 180937], |r| {
            r.get::<_, String>(3)
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for target in [1_000, 10_000, 100_000]
        .into_iter()
        .filter(|n| *n <= maximum)
    {
        let batch_start = Instant::now();
        while ids.len() < target {
            let first = ids.len();
            let count = 8.min(target - first);
            let summaries: Vec<_> = (first..first + count)
                .map(|n| {
                    format!(
                        "Synthetic record R{n:06} stores inspection value V{:06}.",
                        n * 7 + 11
                    )
                })
                .collect();
            let path = format!("source-{first:06}.md");
            let raw = summaries.join("\n");
            fs::write(root.join(&path), &raw)?;
            let hash = hex::encode(native::sha(raw.as_bytes()));
            let mut updates = Vec::new();
            let mut batch_vectors = Vec::new();
            db.conn.execute_batch("BEGIN IMMEDIATE")?;
            for (i, summary) in summaries.iter().enumerate() {
                let n = first + i;
                let saved = admin::execute(
                    &k,
                    &json!({"action":"remember-bound","arguments":{"type":"semantic",
                    "subject":format!("Inspection R{n:06}"),"summary":summary,"source":path,"source_hash":hash}}),
                )?;
                let id = saved["memory_id"]
                    .as_str()
                    .ok_or("missing memory identity")?
                    .to_owned();
                let (row, _) = db.memory(&session.scope, &id, true)?;
                let v = vector(n as u64 + 1);
                updates.push(json!({"id":id,"checksum":hex::encode(row.bytes("record_checksum")?),"vector":v}));
                batch_vectors.extend_from_slice(&v);
                all.extend(v);
                ids.push(id);
            }
            let result = vectors::execute(
                &k,
                "vector-put",
                &json!({"model":"synthetic-scale-v1","items":updates}),
            )?;
            assert_eq!(result["stored"], count);
            db.conn.execute_batch("COMMIT")?;
            oracle.add_with_ids(
                &batch_vectors,
                &(first as u64..(first + count) as u64).collect::<Vec<_>>(),
            )?;
        }
        let seeded = batch_start.elapsed().as_secs_f64();
        let mut search_ms = Vec::new();
        let mut builds = Vec::new();
        let mut exact_hits = 0;
        let mut native_hits = 0;
        for n in [0, target / 7, target / 3, target - 1] {
            let query = &all[n * DIM..(n + 1) * DIM];
            let best = all
                .as_chunks::<DIM>()
                .0
                .iter()
                .enumerate()
                .map(|(id, v)| (id, v.iter().zip(query).map(|(a, b)| a * b).sum::<f32>()))
                .max_by(|a, b| a.1.total_cmp(&b.1))
                .unwrap()
                .0;
            let clock = Instant::now();
            let (_, candidates) = oracle.search(query, 64);
            exact_hits += usize::from(candidates.contains(&(best as u64)));
            let found = vectors::execute(
                &k,
                "vector-recall",
                &json!({"model":"synthetic-scale-v1","query":"synthetic capacity probe","vector":query}),
            )?;
            search_ms.push(clock.elapsed().as_secs_f64() * 1000.0);
            builds.push(found["index_build"].clone());
            native_hits += usize::from(
                found["memories"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|r| r["id"] == ids[n]),
            );
        }
        assert_eq!(exact_hits, 4);
        assert_eq!(native_hits, 4);
        assert_eq!(
            vectors::execute(&k, "vector-status", &json!({}))?["pending"],
            0
        );
        let change = target / 7;
        oracle.remove(change as u64);
        let replacement = vector(1_000_000 + target as u64);
        oracle.add_with_ids(&replacement, &[change as u64])?;
        let (_, nearest) = oracle.search(&replacement, 1);
        assert_eq!(nearest[0], change as u64);
        oracle.remove(change as u64);
        oracle.add_with_ids(&all[change * DIM..(change + 1) * DIM], &[change as u64])?;
        println!(
            "{}",
            json!({"records":target,"dimensions":DIM,"seed_seconds":seeded,"elapsed_seconds":started.elapsed().as_secs_f64(),
            "encrypted":db.encrypted(),"source_bound":true,"oracle_hits":exact_hits,"native_hits":native_hits,"queries":4,
            "search_ms":search_ms,"index_builds":builds,"query_plan":plan,"status":vectors::execute(&k,"vector-status",&json!({}))?,"mutation_check":true,
            "limits":["Supplied deterministic vectors, not natural-language relevance or model inference throughput.",
                "Single-process administrative path and explicit 512 MiB index budget, not the default four-reader host."]})
        );
    }
    db.verify_scope(&session.scope)?;
    Ok(())
}
