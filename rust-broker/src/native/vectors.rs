//! SQLCipher-backed derived vectors, durable coalescing jobs and bounded RAM indexes.
use super::{
    Result, canonical,
    database::{field, identifier},
    ensure,
    knowledge::{Knowledge, keys, tokens},
    now, policy, sha,
};
use base64::{Engine, engine::general_purpose::STANDARD};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    sync::{
        Arc, Mutex, MutexGuard, OnceLock,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
};

const MIB: usize = 1024 * 1024;
const BUILD_BATCH_ROWS: usize = 64;
// Pin the identity lookup: without statistics SQLite can choose a quadratic scope scan.
pub const INDEX_ROWS_SQL: &str = "SELECT m.memory_pk,v.memory_id,v.vector,v.vector_checksum FROM native_vector v JOIN cortex_memory m INDEXED BY native_memory_scope_id ON m.memory_id=v.memory_id WHERE v.scope=? AND m.scope=v.scope AND m.status=0 AND m.record_checksum=v.checksum ORDER BY v.memory_id LIMIT ?";
const INDEX_COUNT_SQL: &str = "SELECT count(*) FROM (SELECT 1 FROM native_vector v JOIN cortex_memory m INDEXED BY native_memory_scope_id ON m.memory_id=v.memory_id WHERE v.scope=? AND m.scope=v.scope AND m.status=0 AND m.record_checksum=v.checksum LIMIT ?)";
fn cache_budget() -> Result<usize> {
    let bytes = std::env::var("MEMORYCORE_AI_INDEX_CACHE_BYTES")
        .map_or(Ok(64 * MIB), |s| s.parse::<usize>())?;
    ensure(
        (8 * MIB..=512 * MIB).contains(&bytes),
        "invalid index cache budget",
    )?;
    Ok(bytes)
}
fn resident_bytes(dim: usize, count: usize) -> usize {
    // Packed coordinates and prepared search layout, binary identity maps,
    // allocation slack and rotation setup. This is an estimate, not an OS cap.
    2 * MIB + count.saturating_mul(dim + 512)
}
fn build_bytes(dim: usize, count: usize) -> usize {
    resident_bytes(dim, count) + count.saturating_mul(dim * 4 + 8)
}
fn vector_capacity(dim: usize, budget: usize) -> usize {
    // A build reserves its working set and evicts disposable cached generations
    // as needed; it need not retain two full generations at maximum capacity.
    budget.saturating_sub(2 * MIB) / (dim * 5 + 512 + 8)
}
const MAX_TEXT: usize = 4096;
const CLAIM_JOBS_SQL: &str = "SELECT j.memory_id FROM native_vector_job j LEFT JOIN native_vector_lease l ON l.memory_id=j.memory_id WHERE j.scope=? AND (l.memory_id IS NULL OR (l.expires_ms<? AND l.attempts<5)) ORDER BY j.rowid LIMIT 8";
const OLDEST_JOB_SQL: &str = "SELECT enqueued_ms FROM native_vector_job_age WHERE memory_id=(SELECT memory_id FROM native_vector_job WHERE scope=? ORDER BY rowid LIMIT 1)";
// Calibrated on the pinned local BGE model; not a guarantee of semantic relevance.
const MIN_SIMILARITY: f64 = 0.55;
const BEST_MATCH_BAND: f64 = 0.10;
// Development-calibrated cross-encoder veto, not a probability of answerability.
const MIN_RERANK_SCORE: f64 = -11.0;
const STRONG_SIMILARITY: f64 = 0.62;
const RERANK_DOMINANCE_GAP: f64 = 3.0;

fn initialise(k: &Knowledge) -> Result<()> {
    k.db.conn.execute_batch("CREATE TABLE IF NOT EXISTS native_vector_config(scope TEXT PRIMARY KEY,model TEXT NOT NULL,dim INTEGER NOT NULL,epoch INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS native_vector_job(memory_id BLOB PRIMARY KEY REFERENCES cortex_memory(memory_id) ON DELETE CASCADE,scope TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS native_vector_job_scope ON native_vector_job(scope,memory_id);
        CREATE INDEX IF NOT EXISTS native_vector_job_fifo ON native_vector_job(scope);
        CREATE TABLE IF NOT EXISTS native_vector_job_age(memory_id BLOB PRIMARY KEY REFERENCES native_vector_job(memory_id) ON DELETE CASCADE,enqueued_ms INTEGER NOT NULL);
        CREATE TRIGGER IF NOT EXISTS native_vector_job_enqueued AFTER INSERT ON native_vector_job BEGIN
          INSERT OR REPLACE INTO native_vector_job_age VALUES(new.memory_id,CAST(unixepoch('subsec')*1000 AS INTEGER));
        END;
        INSERT OR IGNORE INTO native_vector_job_age SELECT memory_id,CAST(unixepoch('subsec')*1000 AS INTEGER) FROM native_vector_job;
        CREATE TABLE IF NOT EXISTS native_vector_lease(memory_id BLOB PRIMARY KEY REFERENCES native_vector_job(memory_id) ON DELETE CASCADE,scope TEXT NOT NULL,token TEXT NOT NULL,expires_ms INTEGER NOT NULL,attempts INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS native_vector_lease_scope ON native_vector_lease(scope,expires_ms);
        CREATE TABLE IF NOT EXISTS native_vector(memory_id BLOB PRIMARY KEY REFERENCES cortex_memory(memory_id) ON DELETE CASCADE,scope TEXT NOT NULL,checksum BLOB NOT NULL,vector BLOB NOT NULL,vector_checksum BLOB NOT NULL);
        CREATE INDEX IF NOT EXISTS native_vector_scope ON native_vector(scope,memory_id);
        CREATE TABLE IF NOT EXISTS native_vector_change(seq INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT NOT NULL,memory_id BLOB NOT NULL);
        CREATE INDEX IF NOT EXISTS native_vector_change_scope ON native_vector_change(scope,seq);
        CREATE TRIGGER IF NOT EXISTS native_vector_memory_insert AFTER INSERT ON cortex_memory BEGIN
          INSERT OR REPLACE INTO native_vector_job SELECT new.memory_id,new.scope WHERE EXISTS(SELECT 1 FROM native_vector_config WHERE scope=new.scope) AND new.status=0;
        END;
        CREATE TRIGGER IF NOT EXISTS native_vector_memory_update AFTER UPDATE ON cortex_memory WHEN old.record_checksum IS NOT new.record_checksum OR old.status!=new.status BEGIN
          DELETE FROM native_vector WHERE memory_id=new.memory_id;
          DELETE FROM native_vector_job WHERE memory_id=new.memory_id;
          INSERT INTO native_vector_job SELECT new.memory_id,new.scope WHERE EXISTS(SELECT 1 FROM native_vector_config WHERE scope=new.scope) AND new.status=0;
        END;")?;
    for op in ["INSERT", "UPDATE", "DELETE"] {
        let row = if op == "DELETE" { "old" } else { "new" };
        k.db.conn.execute_batch(&format!("CREATE TRIGGER IF NOT EXISTS native_vector_epoch_{op} AFTER {op} ON native_vector BEGIN UPDATE native_vector_config SET epoch=epoch+1 WHERE scope={row}.scope; END;"))?;
        k.db.conn.execute_batch(&format!("CREATE TRIGGER IF NOT EXISTS native_vector_delta_{op} AFTER {op} ON native_vector BEGIN INSERT INTO native_vector_change(scope,memory_id) VALUES({row}.scope,{row}.memory_id); DELETE FROM native_vector_change WHERE seq<=(SELECT max(seq)-8192 FROM native_vector_change); END;"))?;
    }
    Ok(())
}
fn configured(k: &Knowledge) -> Result<Option<(String, usize, i64)>> {
    let exists: bool = k.db.conn.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='native_vector_config' AND type='table')",[],|r|r.get(0))?;
    if !exists {
        return Ok(None);
    }
    let row =
        k.db.conn
            .query_row(
                "SELECT model,dim,epoch FROM native_vector_config WHERE scope=?",
                [&k.session.scope],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, i64>(1)?,
                        r.get::<_, i64>(2)?,
                    ))
                },
            )
            .optional()?;
    if let Some((model, dim, epoch)) = row {
        ensure(
            (32..=1536).contains(&dim),
            "invalid stored vector dimension",
        )?;
        Ok(Some((model, dim as usize, epoch)))
    } else {
        Ok(None)
    }
}
fn normalise(input: &Value, dim: usize) -> Result<Vec<f32>> {
    ensure((32..=1536).contains(&dim), "vector dimension mismatch")?;
    let values: Vec<f64> = if let Some(array) = input.as_array() {
        ensure(array.len() == dim, "vector dimension mismatch")?;
        array
            .iter()
            .map(|value| {
                value
                    .as_f64()
                    .ok_or_else(|| "numeric vector required".into())
            })
            .collect::<Result<_>>()?
    } else {
        keys(input, &["encoding", "data"], &[])?;
        ensure(
            input["encoding"] == "f32le-base64",
            "unknown vector encoding",
        )?;
        let encoded = field(input, "data")?;
        ensure(
            encoded.len() == (dim * 4).div_ceil(3) * 4,
            "packed vector dimension mismatch",
        )?;
        let bytes = STANDARD.decode(encoded)?;
        ensure(bytes.len() == dim * 4, "packed vector dimension mismatch")?;
        bytes
            .as_chunks::<4>()
            .0
            .iter()
            .map(|b| f32::from_le_bytes(*b) as f64)
            .collect()
    };
    ensure(
        values.len() == dim && (32..=1536).contains(&dim),
        "vector dimension mismatch",
    )?;
    let mut vector = Vec::with_capacity(dim);
    let mut squared = 0.0_f64;
    for value in values {
        ensure(
            value.is_finite() && value.abs() <= 1e6,
            "invalid vector coordinate",
        )?;
        vector.push(value as f32);
        squared += value * value;
    }
    ensure(
        squared > 1e-20 && squared.is_finite(),
        "zero or invalid vector",
    )?;
    let norm = squared.sqrt() as f32;
    for v in &mut vector {
        *v /= norm;
    }
    Ok(vector)
}
pub fn queue_bound(k: &Knowledge, id: &str) -> Result<()> {
    if !k.session.generate_memories || configured(k)?.is_none() {
        return Ok(());
    }
    let queued: bool = k.db.conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM native_vector_job WHERE memory_id=? AND scope=?)",
        params![identifier(id)?, k.session.scope],
        |row| row.get(0),
    )?;
    // Existing work is revalidated when claimed; an idempotent enqueue need
    // not inspect the same source again while holding the writer transaction.
    if !queued && k.freshness(id).is_ok_and(|state| state["state"] == "fresh") {
        k.db.conn.execute("INSERT OR IGNORE INTO native_vector_job SELECT memory_id,scope FROM cortex_memory m WHERE scope=? AND memory_id=? AND status=0 AND NOT EXISTS(SELECT 1 FROM native_vector v WHERE v.memory_id=m.memory_id AND v.checksum=m.record_checksum)",params![k.session.scope,identifier(id)?])?;
    }
    Ok(())
}
pub fn execute(k: &Knowledge, action: &str, a: &Value) -> Result<Value> {
    if action == "vector-status" {
        keys(a, &[], &[])?;
        return Ok(match configured(k)? {
            Some((model, dim, _)) => {
                let pending: i64 = k.db.conn.query_row(
                    "SELECT count(*) FROM native_vector_job WHERE scope=?",
                    [&k.session.scope],
                    |r| r.get(0),
                )?;
                let leases: bool = k.db.conn.query_row(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='native_vector_lease')",
                    [],
                    |r| r.get(0),
                )?;
                let quarantined: i64 = if leases {
                    k.db.conn.query_row(
                        "SELECT count(*) FROM native_vector_lease WHERE scope=? AND attempts>=5",
                        [&k.session.scope],
                        |r| r.get(0),
                    )?
                } else {
                    0
                };
                let has_age: bool = k.db.conn.query_row(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='native_vector_job_age')",
                    [],
                    |r| r.get(0),
                )?;
                let oldest: Option<i64> = if has_age {
                    // The FIFO head is the oldest admitted job, including across
                    // wall-clock adjustments; do not rescan every pending age.
                    k.db.conn
                        .query_row(OLDEST_JOB_SQL, [&k.session.scope], |r| r.get(0))
                        .optional()?
                } else {
                    None
                };
                json!({"configured":true,"model":model,"dimensions":dim,"pending":pending,"quarantined":quarantined,
                    "pending_age_order":"enqueue_sequence",
                    "oldest_pending_age_ms":oldest.map(|t|(chrono::Utc::now().timestamp_millis()-t).max(0)),
                    "index_budget_bytes":cache_budget()?,"estimated_vector_capacity":vector_capacity(dim,cache_budget()?),
                    "cache":cached_status(k,&model,dim)?})
            }
            None => json!({"configured":false}),
        });
    }
    if action == "vector-recall" {
        ensure(k.session.use_memories, "memory recall disabled")?;
        return recall(k, a, false);
    }
    ensure(k.session.generate_memories, "memory indexing disabled")?;
    match action {
        "vector-configure" => {
            keys(a, &["model", "dimensions"], &[])?;
            let model = field(a, "model")?;
            policy::check_text(model, 200)?;
            ensure(!model.trim().is_empty(), "model identity required")?;
            let dim = a["dimensions"].as_u64().ok_or("dimensions required")?;
            ensure((32..=1536).contains(&dim), "unsupported dimensions")?;
            initialise(k)?;
            if configured(k)?.is_some_and(|(m, d, _)| m == model && d == dim as usize) {
                return Ok(json!({"configured":true,"changed":false}));
            }
            k.db.conn.execute("INSERT INTO native_vector_config(scope,model,dim,epoch) VALUES(?,?,?,1) ON CONFLICT(scope) DO UPDATE SET model=excluded.model,dim=excluded.dim,epoch=native_vector_config.epoch+1",params![k.session.scope,model,dim as i64])?;
            k.db.conn.execute(
                "DELETE FROM native_vector WHERE scope=?",
                [&k.session.scope],
            )?;
            k.db.conn.execute("INSERT OR REPLACE INTO native_vector_job SELECT memory_id,scope FROM cortex_memory WHERE scope=? AND status=0",[&k.session.scope])?;
            Ok(json!({"configured":true,"changed":true}))
        }
        "vector-jobs" | "vector-claim" => {
            keys(a, &[], &[])?;
            let (model, dim, _) = configured(k)?.ok_or("vector scope not configured")?;
            let claim = action == "vector-claim";
            let millis = chrono::Utc::now().timestamp_millis();
            let sql = if claim {
                CLAIM_JOBS_SQL
            } else {
                "SELECT memory_id FROM native_vector_job WHERE scope=? AND ? IS NOT NULL ORDER BY rowid LIMIT 8"
            };
            let mut statement = k.db.conn.prepare_cached(sql)?;
            let ids = statement
                .query_map(params![k.session.scope, millis], |r| r.get::<_, Vec<u8>>(0))?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            let mut jobs = Vec::new();
            for id in ids {
                if claim
                    && !k
                        .freshness(&hex::encode(&id))
                        .is_ok_and(|state| state["state"] == "fresh")
                {
                    // A later validated binding/lifecycle transition requeues this record.
                    k.db.conn.execute(
                        "DELETE FROM native_vector_job WHERE memory_id=? AND scope=?",
                        params![id, k.session.scope],
                    )?;
                    continue;
                }
                let (row, fields) = k.db.memory(&k.session.scope, &hex::encode(&id), false)?;
                let text = format!("{}\n{}\n{}", fields[0], fields[1], k.db.detail(&row)?);
                let projected: String = text.chars().take(MAX_TEXT).collect();
                let mut job = json!({"id":hex::encode(row.bytes("memory_id")?),"checksum":hex::encode(row.bytes("record_checksum")?),"text":projected,"projection_truncated":text.chars().count()>MAX_TEXT});
                if claim {
                    let token = uuid::Uuid::new_v4().simple().to_string();
                    k.db.conn.execute("INSERT INTO native_vector_lease(memory_id,scope,token,expires_ms,attempts) VALUES(?,?,?,?,1) ON CONFLICT(memory_id) DO UPDATE SET token=excluded.token,expires_ms=excluded.expires_ms,attempts=native_vector_lease.attempts+1",params![id,k.session.scope,token,millis+15_000])?;
                    job["lease"] = json!(token);
                    job["enqueued_ms"] = json!(k.db.conn.query_row(
                        "SELECT enqueued_ms FROM native_vector_job_age WHERE memory_id=?",
                        [&id],
                        |r| r.get::<_, i64>(0),
                    )?);
                    job["claimed_ms"] = json!(millis);
                }
                jobs.push(job);
            }
            Ok(json!({"model":model,"dimensions":dim,"jobs":jobs}))
        }
        "vector-release" => {
            keys(a, &["items", "failed"], &[])?;
            let items = a["items"].as_array().ok_or("items required")?;
            ensure(items.len() <= 8, "release batch bound")?;
            let failed = a["failed"].as_bool().ok_or("failed flag required")?;
            let millis = chrono::Utc::now().timestamp_millis();
            let mut released = 0;
            for item in items {
                keys(item, &["id", "lease"], &[])?;
                released += k.db.conn.execute("UPDATE native_vector_lease SET expires_ms=?,attempts=max(0,attempts-?) WHERE memory_id=? AND scope=? AND token=? AND expires_ms>0",params![if failed {millis+2000} else {0},i64::from(!failed),identifier(field(item,"id")?)?,k.session.scope,field(item,"lease")?])?;
            }
            Ok(json!({"released":released}))
        }
        "vector-retry" => {
            keys(a, &["ids"], &[])?;
            let ids = a["ids"].as_array().ok_or("ids required")?;
            ensure(ids.len() <= 8, "retry batch bound")?;
            let mut reset = 0;
            for id in ids {
                reset += k.db.conn.execute("UPDATE native_vector_lease SET expires_ms=0,attempts=0 WHERE memory_id=? AND scope=? AND attempts>=5",params![identifier(id.as_str().ok_or("id required")?)?,k.session.scope])?;
            }
            Ok(json!({"reset":reset}))
        }
        "vector-put" => {
            keys(a, &["model", "items"], &[])?;
            let (model, dim, _) = configured(k)?.ok_or("vector scope not configured")?;
            ensure(a["model"] == model, "embedding model changed")?;
            let items = a["items"].as_array().ok_or("vector items required")?;
            ensure((1..=8).contains(&items.len()), "vector batch bound")?;
            let mut stored = 0;
            let mut stale = 0;
            let mut indexing_enqueued_ms = Vec::new();
            for item in items {
                keys(item, &["id", "checksum", "vector"], &["lease"])?;
                let id = identifier(field(item, "id")?)?;
                let lease: Option<(String,i64)> =
                    k.db.conn
                        .query_row(
                            "SELECT token,expires_ms FROM native_vector_lease WHERE memory_id=? AND scope=?",
                            params![id, k.session.scope],
                            |r| Ok((r.get(0)?,r.get(1)?)),
                        )
                        .optional()?;
                if lease.as_ref().map(|(token, _)| token.as_str()) != item["lease"].as_str()
                    || lease.is_some_and(|(_, expires)| {
                        expires <= chrono::Utc::now().timestamp_millis()
                    })
                {
                    stale += 1;
                    continue;
                }
                let current = k.db.memory(&k.session.scope, field(item, "id")?, false);
                // Deleted/superseded work is acknowledged without resurrecting its content.
                let Ok((row, _)) = current else {
                    stale += 1;
                    continue;
                };
                if row.int("status")? != 0
                    || hex::encode(row.bytes("record_checksum")?) != field(item, "checksum")?
                {
                    stale += 1;
                    continue;
                }
                let vector = normalise(&item["vector"], dim)?;
                let bytes: Vec<u8> = vector.iter().flat_map(|v| v.to_le_bytes()).collect();
                k.db.conn.execute("INSERT INTO native_vector VALUES(?,?,?,?,?) ON CONFLICT(memory_id) DO UPDATE SET checksum=excluded.checksum,vector=excluded.vector,vector_checksum=excluded.vector_checksum WHERE native_vector.checksum!=excluded.checksum OR native_vector.vector_checksum!=excluded.vector_checksum",params![id,k.session.scope,row.bytes("record_checksum")?,bytes,sha(&bytes)])?;
                let enqueued: Option<i64> =
                    k.db.conn
                        .query_row(
                            "SELECT enqueued_ms FROM native_vector_job_age WHERE memory_id=?",
                            [&id],
                            |r| r.get(0),
                        )
                        .optional()?;
                indexing_enqueued_ms.push(enqueued);
                k.db.conn.execute(
                    "DELETE FROM native_vector_job WHERE memory_id=? AND scope=?",
                    params![id, k.session.scope],
                )?;
                stored += 1;
            }
            Ok(json!({"stored":stored,"stale":stale,"indexing_enqueued_ms":indexing_enqueued_ms}))
        }
        _ => Err("unknown vector action".into()),
    }
}

#[cfg(test)]
mod budget_tests {
    use super::*;
    #[test]
    fn preview_reuses_only_its_own_freshness_and_skips_unanswerable_source_reads() {
        let root =
            std::env::temp_dir().join(format!("memorycore-ai-preview-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&root).unwrap();
        let db = super::super::database::Database::initialize(
            rusqlite::Connection::open_in_memory().unwrap(),
        )
        .unwrap();
        Knowledge::initialize(&db).unwrap();
        let session: crate::Session = serde_json::from_value(json!({
            "id":"preview","scope":"synthetic:preview","source_root":root,
        }))
        .unwrap();
        let k = Knowledge::new(&db, &session).unwrap();
        execute(
            &k,
            "vector-configure",
            &json!({"model":"synthetic-preview-v1","dimensions":32}),
        )
        .unwrap();
        let mut vector = vec![0.0_f32; 32];
        vector[0] = 1.0;
        let raw: Vec<u8> = vector
            .iter()
            .flat_map(|value| value.to_le_bytes())
            .collect();
        let mut ids = Vec::new();
        for (path, summary) in [
            ("frequency.md", "The coffee appliance is descaled monthly."),
            (
                "inventory.md",
                "The coffee appliance inventory number is 1423.",
            ),
        ] {
            std::fs::write(root.join(path), summary).unwrap();
            let source_hash = hex::encode(sha(summary.as_bytes()));
            let saved = db
                .remember(
                    &session.scope,
                    &json!({"type":"semantic","subject":"Coffee maintenance",
                "summary":summary,"source":path,"source_hash":source_hash}),
                )
                .unwrap();
            let id = saved["memory_id"].as_str().unwrap();
            k.bind(id, path, &source_hash).unwrap();
            let (row, _) = db.memory(&session.scope, id, true).unwrap();
            db.conn
                .execute(
                    "INSERT INTO native_vector VALUES(?,?,?,?,?)",
                    params![
                        identifier(id).unwrap(),
                        session.scope,
                        row.bytes("record_checksum").unwrap(),
                        raw,
                        sha(&raw)
                    ],
                )
                .unwrap();
            ids.push(id.to_owned());
        }
        let query = json!({"query":"How often should the coffee appliance be descaled?",
            "model":"synthetic-preview-v1","vector":vector});
        let first = recall(&k, &query, true).unwrap();
        assert_eq!(first["memories"].as_array().unwrap().len(), 1);
        assert_eq!(first["memories"][0]["id"], ids[0]);
        assert_eq!(first["candidate_source_checks"], 1);
        assert_eq!(first["preview_freshness_reused"], 1);
        assert_eq!(first["answerability_filtered"], 1);
        std::fs::write(root.join("frequency.md"), "The source has changed.").unwrap();
        let next = recall(&k, &query, true).unwrap();
        assert!(next["memories"].as_array().unwrap().is_empty());
        for path in ["frequency.md", "inventory.md"] {
            std::fs::remove_file(root.join(path)).unwrap();
        }
        std::fs::remove_dir(root).unwrap();
    }

    #[test]
    fn oldest_job_lookup_uses_fifo_index_and_preserves_scope_and_empty_results() {
        let db = super::super::database::Database::initialize(
            rusqlite::Connection::open_in_memory().unwrap(),
        )
        .unwrap();
        Knowledge::initialize(&db).unwrap();
        let session: crate::Session =
            serde_json::from_value(json!({"id":"fifo","scope":"synthetic:fifo","source_root":"."}))
                .unwrap();
        let k = Knowledge::new(&db, &session).unwrap();
        initialise(&k).unwrap();
        let mut ids = Vec::new();
        for (index, scope, age) in [
            (0, "synthetic:fifo", 1000),
            (1, "synthetic:other", 0),
            (2, "synthetic:fifo", 500),
        ] {
            let saved = db.remember(scope,&json!({"type":"semantic","subject":format!("FIFO {index}"),"summary":"Synthetic queue fixture."})).unwrap();
            let id = identifier(saved["memory_id"].as_str().unwrap()).unwrap();
            db.conn
                .execute(
                    "INSERT INTO native_vector_job VALUES(?,?)",
                    params![id, scope],
                )
                .unwrap();
            db.conn
                .execute(
                    "UPDATE native_vector_job_age SET enqueued_ms=? WHERE memory_id=?",
                    params![age, id],
                )
                .unwrap();
            ids.push(id);
        }
        let oldest = |scope| {
            db.conn
                .query_row(OLDEST_JOB_SQL, [scope], |r| r.get::<_, i64>(0))
                .optional()
                .unwrap()
        };
        assert_eq!(oldest("synthetic:fifo"), Some(1000));
        assert_eq!(oldest("synthetic:other"), Some(0));
        assert_eq!(oldest("synthetic:empty"), None);
        db.conn
            .execute("DELETE FROM native_vector_job WHERE memory_id=?", [&ids[0]])
            .unwrap();
        assert_eq!(oldest("synthetic:fifo"), Some(500));
        let details = db
            .conn
            .prepare(&format!("EXPLAIN QUERY PLAN {OLDEST_JOB_SQL}"))
            .unwrap()
            .query_map(["synthetic:fifo"], |r| r.get::<_, String>(3))
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap()
            .join(" ");
        assert!(
            details.contains("native_vector_job_fifo (scope=?)"),
            "{details}"
        );
        assert!(!details.contains("TEMP B-TREE"), "{details}");
    }

    #[test]
    fn packed_float32_preserves_coordinates_and_rejects_invalid_encodings() {
        let vector: Vec<f32> = (0..64).map(|n| (n as f32 - 31.0) / 19.0).collect();
        let raw: Vec<u8> = vector.iter().flat_map(|v| v.to_le_bytes()).collect();
        let packed = json!({"encoding":"f32le-base64","data":STANDARD.encode(&raw)});
        assert_eq!(
            normalise(&packed, 64).unwrap(),
            normalise(&json!(vector), 64).unwrap()
        );
        for value in [f32::NAN, f32::INFINITY, f32::NEG_INFINITY, 1e7, 0.0] {
            let raw: Vec<u8> = [value; 64].iter().flat_map(|v| v.to_le_bytes()).collect();
            assert!(
                normalise(
                    &json!({"encoding":"f32le-base64","data":STANDARD.encode(raw)}),
                    64
                )
                .is_err()
            );
        }
        for bad in [
            json!(null),
            json!({"encoding":"unknown","data":packed["data"]}),
            json!({"encoding":"f32le-base64","data":STANDARD.encode(&raw[..255])}),
            json!({"encoding":"f32le-base64","data":"!".repeat(344)}),
            json!({"encoding":"f32le-base64","data":packed["data"],"extra":1}),
        ] {
            assert!(normalise(&bad, 64).is_err());
        }
    }

    #[test]
    fn vector_loading_uses_scoped_identity_lookup_after_initialise_and_upgrade() {
        let db = super::super::database::Database::initialize(
            rusqlite::Connection::open_in_memory().unwrap(),
        )
        .unwrap();
        Knowledge::initialize(&db).unwrap();
        let session: crate::Session =
            serde_json::from_value(json!({"id":"plan","scope":"synthetic:plan","source_root":"."}))
                .unwrap();
        initialise(&Knowledge::new(&db, &session).unwrap()).unwrap();
        for upgrade in [false, true] {
            if upgrade {
                db.conn
                    .execute_batch("DROP INDEX native_memory_scope_id")
                    .unwrap();
                Knowledge::initialize(&db).unwrap();
            }
            let details = db
                .conn
                .prepare(&format!("EXPLAIN QUERY PLAN {INDEX_ROWS_SQL}"))
                .unwrap()
                .query_map(params!["synthetic:plan", 100_001], |r| {
                    r.get::<_, String>(3)
                })
                .unwrap()
                .collect::<rusqlite::Result<Vec<_>>>()
                .unwrap()
                .join(" ");
            assert!(
                details.contains("native_memory_scope_id (scope=? AND memory_id=?)"),
                "{details}"
            );
            assert!(!details.contains("cortex_scope_status"), "{details}");
            assert!(!details.contains("TEMP B-TREE"), "{details}");
        }
    }
    #[test]
    fn capacity_accounts_for_dimensions_and_rebuild_scratch() {
        assert!(vector_capacity(384, 64 * MIB) > 10_000);
        assert!(vector_capacity(384, 512 * MIB) > 100_000);
        assert!(vector_capacity(1536, 64 * MIB) < vector_capacity(384, 64 * MIB));
        for dim in [32, 384, 1536] {
            for budget in [8 * MIB, 64 * MIB, 512 * MIB] {
                let n = vector_capacity(dim, budget);
                assert!(build_bytes(dim, n) <= budget);
                assert!(build_bytes(dim, n + 1) > budget);
            }
        }
        assert!(vector_capacity(384, 254_682_470) >= 100_000);
    }

    fn empty_cached_index(partition: &str, retained_rows: usize) -> CachedIndex {
        CachedIndex {
            partition: partition.into(),
            key: partition.into(),
            index: turbovec::IdMapIndex::new(32, 4).unwrap(),
            ids: BTreeMap::new(),
            reverse: BTreeMap::new(),
            retained_rows,
            model: "synthetic".into(),
            dim: 32,
            cursor: 0,
            epoch: 0,
            build: json!({}),
        }
    }

    #[test]
    fn cache_reservations_evict_and_prevent_double_spending() {
        let cache = Arc::new(Mutex::new(IndexCache::default()));
        cache
            .lock()
            .unwrap()
            .entries
            .push_back(empty_cached_index("old", 10_000));
        let reserved = BuildReservation::acquire(cache.clone(), 6 * MIB, 8 * MIB).unwrap();
        {
            let mut state = cache.lock().unwrap();
            assert!(state.entries.is_empty());
            assert_eq!(state.reserved_bytes, 6 * MIB);
            assert!(!state.make_room(3 * MIB, 8 * MIB));
        }
        assert!(BuildReservation::acquire(cache.clone(), 3 * MIB, 8 * MIB).is_err());
        drop(reserved);
        assert_eq!(cache.lock().unwrap().reserved_bytes, 0);
        let retry = BuildReservation::acquire(cache.clone(), 8 * MIB, 8 * MIB).unwrap();
        drop(retry);
        assert_eq!(cache.lock().unwrap().reserved_bytes, 0);
    }

    #[test]
    fn failed_build_releases_reservation() {
        let cache = Arc::new(Mutex::new(IndexCache::default()));
        let failed = (|| -> Result<()> {
            let _reservation = BuildReservation::acquire(cache.clone(), 8 * MIB, 8 * MIB)?;
            Err("synthetic construction failure".into())
        })();
        assert!(failed.is_err());
        assert_eq!(cache.lock().unwrap().reserved_bytes, 0);
    }

    #[test]
    fn publication_exchanges_reservation_and_rejects_late_generation() {
        let cache = Arc::new(Mutex::new(IndexCache::default()));
        let reservation = BuildReservation::acquire(cache.clone(), 4 * MIB, 8 * MIB).unwrap();
        let mut newer = empty_cached_index("same", 20);
        newer.epoch = 2;
        publish(PendingIndex {
            entry: newer,
            reservation,
        })
        .unwrap();
        {
            let state = cache.lock().unwrap();
            assert_eq!(state.reserved_bytes, 0);
            assert_eq!(state.resident_bytes(), resident_bytes(32, 20));
        }
        let reservation = BuildReservation::acquire(cache.clone(), 4 * MIB, 8 * MIB).unwrap();
        publish(PendingIndex {
            entry: empty_cached_index("same", 30),
            reservation,
        })
        .unwrap();
        let state = cache.lock().unwrap();
        assert_eq!(state.entries.len(), 1);
        assert_eq!(state.entries[0].epoch, 2);
        assert_eq!(state.reserved_bytes, 0);
    }

    #[test]
    fn removed_rows_do_not_refund_retained_allocations() {
        let entry = empty_cached_index("removed", 10_000);
        assert!(entry.ids.is_empty());
        assert_eq!(entry.resident_bytes(), resident_bytes(32, 10_000));
    }

    #[test]
    fn missed_delta_rebuild_and_model_switch_reject_old_publication() {
        let db = super::super::database::Database::initialize(
            rusqlite::Connection::open_in_memory().unwrap(),
        )
        .unwrap();
        Knowledge::initialize(&db).unwrap();
        let session: crate::Session = serde_json::from_value(
            json!({"id":"generation","scope":"synthetic:generation","source_root":"."}),
        )
        .unwrap();
        let k = Knowledge::new(&db, &session).unwrap();
        execute(
            &k,
            "vector-configure",
            &json!({"model":"synthetic-v1","dimensions":32}),
        )
        .unwrap();
        let saved = db.remember(&session.scope, &json!({"type":"semantic","subject":"Generation fixture","summary":"Synthetic data."})).unwrap();
        let id = identifier(saved["memory_id"].as_str().unwrap()).unwrap();
        let (row, _) = db
            .memory(&session.scope, saved["memory_id"].as_str().unwrap(), true)
            .unwrap();
        let mut query = vec![0.0_f32; 32];
        query[0] = 1.0;
        let raw: Vec<u8> = query.iter().flat_map(|v| v.to_le_bytes()).collect();
        db.conn
            .execute(
                "INSERT INTO native_vector VALUES(?,?,?,?,?)",
                params![
                    id,
                    session.scope,
                    row.bytes("record_checksum").unwrap(),
                    raw,
                    sha(&raw)
                ],
            )
            .unwrap();
        let (model, dim, epoch) = configured(&k).unwrap().unwrap();
        let (partition, key, cursor) = index_identity(&k, &model, dim, epoch).unwrap();
        let late = build_index(
            &k,
            &model,
            dim,
            partition.clone(),
            key,
            (cursor, epoch),
            false,
        )
        .unwrap();
        assert!(!candidates(&k, &model, dim, epoch, &query).unwrap().1);
        db.conn.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<8193) INSERT INTO native_vector_change(scope,memory_id) SELECT ?,? FROM n", params![session.scope,id]).unwrap();
        db.conn.execute_batch("DELETE FROM native_vector_change WHERE seq<=(SELECT max(seq)-8192 FROM native_vector_change)").unwrap();
        db.conn
            .execute(
                "UPDATE native_vector SET vector_checksum=vector_checksum WHERE memory_id=?",
                [&id],
            )
            .unwrap();
        let (_, _, next_epoch) = configured(&k).unwrap().unwrap();
        let rebuilt = candidates(&k, &model, dim, next_epoch, &query).unwrap();
        assert!(!rebuilt.1);
        assert_eq!(rebuilt.2, 0);
        assert!(candidates(&k, &model, dim, next_epoch, &query).unwrap().1);
        execute(
            &k,
            "vector-configure",
            &json!({"model":"synthetic-v2","dimensions":32}),
        )
        .unwrap();
        let (new_model, _, new_epoch) = configured(&k).unwrap().unwrap();
        assert!(
            candidates(&k, &new_model, dim, new_epoch, &query)
                .unwrap()
                .0
                .is_empty()
        );
        publish(late).unwrap();
        let mut cache = CACHE.get().unwrap().lock().unwrap();
        let current = cache
            .entries
            .iter()
            .find(|entry| entry.partition == partition)
            .unwrap();
        assert_eq!(current.model, new_model);
        assert_eq!(current.epoch, new_epoch);
        cache.entries.retain(|entry| entry.partition != partition);
    }
}

pub fn compact(k: &Knowledge, query: &str, embedding: &Value) -> Result<Value> {
    ensure(k.session.use_memories, "memory recall disabled")?;
    keys(
        embedding,
        &[],
        &["model", "vector", "phase", "selection", "reranker"],
    )?;
    policy::check_text(query, 4096)?;
    if embedding["phase"] == "select" {
        return select(k, query, &embedding["selection"]);
    }
    let shortlist = embedding["phase"] == "candidates";
    let mut args = embedding.clone();
    args.as_object_mut().unwrap().remove("phase");
    args.as_object_mut().unwrap().remove("reranker");
    args["query"] = json!(query);
    let mut result = recall(k, &args, shortlist)?;
    if shortlist {
        let model = configured(k)?.map(|(m, _, _)| m);
        let vault: String =
            k.db.conn
                .query_row("SELECT vault_id FROM vault_state", [], |r| r.get(0))?;
        let mut items = Vec::new();
        let mut documents = Vec::new();
        for item in result["memories"].as_array().unwrap() {
            let (row, p) = k.db.memory(&k.session.scope, field(item, "id")?, true)?;
            items.push(
                json!({"id":item["id"],"checksum":hex::encode(row.bytes("record_checksum")?),"similarity":item["similarity"]}),
            );
            // Score only the evidence that can be delivered, not unseen detail.
            documents.push(
                format!("{}\n{}", p[0], p[1])
                    .chars()
                    .take(MAX_TEXT)
                    .collect::<String>(),
            );
        }
        let context = json!({"vault":vault,"scope":k.session.scope,"query":hex::encode(sha(query.as_bytes())),"model":model,"reranker":embedding["reranker"],"items":items});
        let binding = hex::encode(sha(canonical(&context, false)?.as_bytes()));
        result.as_object_mut().unwrap().remove("memories");
        return Ok(
            json!({"context":context,"binding":binding,"documents":documents,"telemetry":result}),
        );
    }
    result["excluded"] = json!({"inactive_or_unfresh":result["excluded_inactive_or_unfresh"]});
    result["omitted"] = json!(0);
    result["candidates_capped"] = result["lexical_candidates_capped"].clone();
    k.compact_recall(&result, "vector_lexical", 1240)
}

fn select(k: &Knowledge, query: &str, selection: &Value) -> Result<Value> {
    keys(selection, &["context", "binding", "scores"], &[])?;
    let context = &selection["context"];
    keys(
        context,
        &["vault", "scope", "query", "model", "reranker", "items"],
        &[],
    )?;
    let vault: String =
        k.db.conn
            .query_row("SELECT vault_id FROM vault_state", [], |r| r.get(0))?;
    let model = configured(k)?.map(|(m, _, _)| m);
    ensure(
        context["vault"] == vault
            && context["scope"] == k.session.scope
            && context["query"] == hex::encode(sha(query.as_bytes()))
            && context["model"] == json!(model)
            && selection["binding"] == hex::encode(sha(canonical(context, false)?.as_bytes())),
        "recall selection context changed",
    )?;
    let items = context["items"]
        .as_array()
        .ok_or("candidate identities required")?;
    ensure(items.len() <= 16, "candidate bound")?;
    let scores = selection["scores"].as_array();
    ensure(
        selection["scores"].is_null() || scores.is_some(),
        "score array or null required",
    )?;
    ensure(
        scores.is_none_or(|s| {
            s.len() == items.len()
                && s.iter()
                    .all(|v| v.is_null() || v.as_f64().is_some_and(f64::is_finite))
        }),
        "invalid reranker scores",
    )?;
    let mut min_score = MIN_RERANK_SCORE;
    let mut dominance_gap = RERANK_DOMINANCE_GAP;
    if scores.is_some() {
        let identity = field(context, "reranker")?;
        policy::check_text(identity, 200)?;
        ensure(
            identity.starts_with("minilm-reranker:")
                || identity.starts_with("mxbai-xsmall-reranker:")
                || identity.starts_with("synthetic-"),
            "reranker identity required",
        )?;
        if identity.starts_with("mxbai-xsmall-reranker:") {
            min_score = -4.0;
            dominance_gap = 1.5;
        }
    }
    let mut ranked = Vec::new();
    let mut excluded = 0;
    let mut seen = std::collections::BTreeSet::new();
    for (i, item) in items.iter().enumerate() {
        keys(item, &["id", "checksum", "similarity"], &[])?;
        let id = field(item, "id")?;
        ensure(seen.insert(id), "duplicate candidate")?;
        let Ok((row, p)) = k.db.memory(&k.session.scope, id, true) else {
            excluded += 1;
            continue;
        };
        if item["checksum"] != hex::encode(row.bytes("record_checksum")?) {
            excluded += 1;
            continue;
        }
        let exact = [p[0].as_str(), p[1].as_str()]
            .iter()
            .any(|text| text.trim().eq_ignore_ascii_case(query.trim()));
        let verified_score = scores.and_then(|s| s[i].as_f64());
        let score = verified_score.unwrap_or(0.0);
        let cosine = item["similarity"].as_f64();
        if !exact
            && scores.is_some()
            && (verified_score.is_none()
                || (score < min_score && cosine.is_none_or(|s| s < STRONG_SIMILARITY))
                || cosine.map_or(score < 0.0, |s| s < MIN_SIMILARITY))
        {
            continue;
        }
        if !super::answerability::supports_requested_field(query, &format!("{}\n{}", p[0], p[1])) {
            continue;
        }
        let freshness = k.freshness(id)?;
        if freshness["state"] != "fresh" {
            excluded += 1;
            continue;
        }
        ranked.push((cosine.unwrap_or(1.0),score,json!({"id":id,"subject":p[0],"summary":p[1],"source":p[4],"source_hash":row.value("source_hash")?,"confidence":row.int("confidence")? as f64/255.0,"freshness":freshness,
            "observed_at":row.text("observed_at")?,"valid_from":if row.optional("valid_from")?==Some(row.text("observed_at")?){Value::Null}else{row.value("valid_from")?},"valid_to":row.value("valid_to")?,"confidence_reason":row.text("confidence_reason")?})));
    }
    ranked.sort_by(|a, b| {
        b.0.total_cmp(&a.0)
            .then_with(|| a.2["id"].as_str().cmp(&b.2["id"].as_str()))
    });
    let mut content = std::collections::BTreeSet::new();
    let best = ranked.first().map_or(-1.0, |r| r.0);
    let focused_subject = if super::answerability::multiple_topics(query) {
        None
    } else {
        ranked
            .iter()
            .filter(|r| r.0 >= best - BEST_MATCH_BAND)
            .max_by(|a, b| a.1.total_cmp(&b.1).then_with(|| a.0.total_cmp(&b.0)))
            // Weak negative logits do not justify overriding the semantic winner.
            // Admission, answerability and freshness were checked above.
            .filter(|r| r.1 >= 0.0)
            .or_else(|| ranked.first())
            .map(|r| r.2["subject"].clone())
    };
    // Remove a secondary topic only when both independent rankers prefer another.
    // Same-subject evidence may contain qualifications or unresolved contradictions.
    let dominated: BTreeSet<_> = ranked
        .iter()
        .filter(|candidate| {
            ranked.iter().any(|other| {
                other.0 > candidate.0
                    && other.1 >= 0.0
                    && other.1 >= candidate.1 + dominance_gap
                    && other.2["subject"] != candidate.2["subject"]
            })
        })
        .filter_map(|r| r.2["id"].as_str().map(str::to_owned))
        .collect();
    let rows: Vec<Value> = ranked
        .into_iter()
        .filter(|(score, _, r)| {
            *score >= best - BEST_MATCH_BAND
                && !dominated.contains(r["id"].as_str().unwrap())
                && focused_subject
                    .as_ref()
                    .is_none_or(|subject| *subject == r["subject"])
        })
        .map(|(_, _, r)| r)
        .filter(|r| {
            content.insert((
                r["subject"].clone().to_string(),
                r["summary"].clone().to_string(),
            ))
        })
        .collect();
    let omitted = rows.len().saturating_sub(8);
    k.compact_recall(&json!({"memories":rows.into_iter().take(8).collect::<Vec<_>>(),"excluded":{"inactive_or_unfresh":excluded},"omitted":omitted}),"verified_hybrid",1240)
}

struct CachedIndex {
    partition: String,
    key: String,
    index: turbovec::IdMapIndex,
    ids: BTreeMap<u64, [u8; 16]>,
    reverse: BTreeMap<[u8; 16], u64>,
    retained_rows: usize,
    model: String,
    dim: usize,
    cursor: i64,
    epoch: i64,
    build: Value,
}
impl CachedIndex {
    fn resident_bytes(&self) -> usize {
        // Removal need not release the dependency's allocated capacity.
        resident_bytes(self.dim, self.retained_rows)
    }
}
#[derive(Default)]
struct IndexCache {
    entries: VecDeque<CachedIndex>,
    reserved_bytes: usize,
}
impl IndexCache {
    fn resident_bytes(&self) -> usize {
        self.entries.iter().map(CachedIndex::resident_bytes).sum()
    }
    fn make_room(&mut self, bytes: usize, budget: usize) -> bool {
        if self.reserved_bytes.saturating_add(bytes) > budget {
            return false;
        }
        while !self.entries.is_empty()
            && self.resident_bytes() + self.reserved_bytes + bytes > budget
        {
            self.entries.pop_front();
        }
        true
    }
}
static CACHE: OnceLock<Arc<Mutex<IndexCache>>> = OnceLock::new();
static BUILD_LOCK: Mutex<()> = Mutex::new(());
fn index_cache() -> &'static Arc<Mutex<IndexCache>> {
    CACHE.get_or_init(|| Arc::new(Mutex::new(IndexCache::default())))
}
fn lock_cache() -> Result<MutexGuard<'static, IndexCache>> {
    index_cache()
        .lock()
        .map_err(|_| "vector cache unavailable".into())
}
struct BuildReservation {
    cache: Arc<Mutex<IndexCache>>,
    bytes: usize,
}
impl BuildReservation {
    fn acquire(cache: Arc<Mutex<IndexCache>>, bytes: usize, budget: usize) -> Result<Self> {
        {
            let mut state = cache.lock().map_err(|_| "vector cache unavailable")?;
            ensure(state.make_room(bytes, budget), "vector index warming")?;
            state.reserved_bytes += bytes;
        }
        Ok(Self { cache, bytes })
    }
}
impl Drop for BuildReservation {
    fn drop(&mut self) {
        if self.bytes != 0 {
            // Recover accounting on unwind even when another cache user panicked.
            let mut state = self.cache.lock().unwrap_or_else(|error| error.into_inner());
            state.reserved_bytes -= self.bytes;
        }
    }
}
struct PendingIndex {
    entry: CachedIndex,
    reservation: BuildReservation,
}
struct Warmer {
    sender: mpsc::SyncSender<crate::Session>,
    pending: Arc<Mutex<BTreeSet<String>>>,
    failed: Arc<Mutex<BTreeMap<String, (String, bool)>>>,
    stopped: Arc<AtomicBool>,
}
static WARMER: OnceLock<Warmer> = OnceLock::new();
pub struct WarmService {
    stopped: Arc<AtomicBool>,
    worker: Option<std::thread::JoinHandle<()>>,
}
impl Drop for WarmService {
    fn drop(&mut self) {
        self.stopped.store(true, Ordering::Relaxed);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}
pub fn start_warmer(config: &crate::Config) -> WarmService {
    let (sender, receiver) = mpsc::sync_channel::<crate::Session>(16);
    let pending = Arc::new(Mutex::new(BTreeSet::new()));
    let stopped = Arc::new(AtomicBool::new(false));
    let failed = Arc::new(Mutex::new(BTreeMap::new()));
    let _ = WARMER.set(Warmer {
        sender,
        pending: pending.clone(),
        failed: failed.clone(),
        stopped: stopped.clone(),
    });
    let path = config.database.clone();
    let key = config.key_env.clone();
    let stop = stopped.clone();
    let worker = std::thread::spawn(move || {
        // Pure model-dimension setup is independent of stored facts.
        turbovec::expected_codebook(4, 384);
        let mut opened = super::database::Database::open_keyed(
            std::path::Path::new(&path),
            true,
            key.as_deref(),
        )
        .ok();
        let mut watched = VecDeque::<crate::Session>::new();
        while !stop.load(Ordering::Relaxed) {
            let (session, requested) =
                match receiver.recv_timeout(std::time::Duration::from_millis(100)) {
                    Ok(session) => {
                        watched.retain(|old| old.scope != session.scope);
                        if watched.len() >= 16 {
                            watched.pop_front();
                        }
                        watched.push_back(session.clone());
                        (session, true)
                    }
                    Err(mpsc::RecvTimeoutError::Timeout) => {
                        let Some(session) = watched.pop_front() else {
                            continue;
                        };
                        watched.push_back(session.clone());
                        if !pending
                            .lock()
                            .is_ok_and(|mut jobs| jobs.insert(session.scope.clone()))
                        {
                            continue;
                        }
                        (session, false)
                    }
                    Err(mpsc::RecvTimeoutError::Disconnected) => break,
                };
            let mut attempted_key = None;
            let built = (|| -> Result<()> {
                if opened.is_none() {
                    opened = Some(super::database::Database::open_keyed(
                        std::path::Path::new(&path),
                        true,
                        key.as_deref(),
                    )?);
                }
                let db = opened.as_ref().unwrap();
                db.conn.execute_batch("BEGIN")?;
                let k = Knowledge::new(db, &session)?;
                let (model, dim, epoch) = configured(&k)?.ok_or("vector scope not configured")?;
                let (partition, key, cursor) = index_identity(&k, &model, dim, epoch)?;
                attempted_key = Some(key.clone());
                {
                    let mut cache = lock_cache()?;
                    // Maintain only resident, previously requested scopes. An
                    // evicted scope must not cause perpetual rebuild churn.
                    if !requested
                        && !cache
                            .entries
                            .iter()
                            .any(|entry| entry.partition == partition)
                    {
                        return Ok(());
                    }
                    if refresh_cached(&k, &model, dim, epoch, &partition, &key, &mut cache)?
                        .is_some()
                    {
                        return Ok(());
                    }
                }
                ensure(
                    !failed.lock().is_ok_and(|errors| {
                        errors
                            .get(&session.scope)
                            .is_some_and(|(failed_key, capacity)| *capacity && failed_key == &key)
                    }),
                    "vector index capacity",
                )?;
                let entry = build_index(&k, &model, dim, partition, key, (cursor, epoch), true)?;
                publish(entry)?;
                Ok(())
            })();
            if let Some(db) = &opened
                && !db.conn.is_autocommit()
            {
                let _ = db.conn.execute_batch("ROLLBACK");
            }
            if let Ok(mut errors) = failed.lock() {
                if let (Err(error), Some(key)) = (built, attempted_key) {
                    errors.insert(
                        session.scope.clone(),
                        (key, error.to_string() == "vector index capacity"),
                    );
                } else {
                    errors.remove(&session.scope);
                }
            }
            if let Ok(mut jobs) = pending.lock() {
                jobs.remove(&session.scope);
            }
        }
    });
    WarmService {
        stopped,
        worker: Some(worker),
    }
}
fn index_identity(
    k: &Knowledge,
    model: &str,
    dim: usize,
    epoch: i64,
) -> Result<(String, String, i64)> {
    let vault: String =
        k.db.conn
            .query_row("SELECT vault_id FROM vault_state", [], |r| r.get(0))?;
    let cursor: i64 = k.db.conn.query_row(
        "SELECT coalesce(max(seq),0) FROM native_vector_change",
        [],
        |r| r.get(0),
    )?;
    Ok((
        canonical(&json!([vault, k.session.scope]), false)?,
        canonical(&json!([vault, k.session.scope, model, dim, epoch]), false)?,
        cursor,
    ))
}
fn cached_status(k: &Knowledge, model: &str, dim: usize) -> Result<Value> {
    let (_, _, epoch) = configured(k)?.ok_or("vector scope not configured")?;
    let (partition, key, _) = index_identity(k, model, dim, epoch)?;
    let cache = lock_cache()?;
    Ok(cache.entries.iter().find(|entry| entry.partition == partition && entry.model == model && entry.dim == dim)
        .map_or_else(|| json!({"current":false,"cached_vectors":0}), |entry|
            json!({"current":entry.key==key,"cached_vectors":entry.ids.len(),"epoch":entry.epoch})))
}
fn publish(pending: PendingIndex) -> Result<()> {
    let PendingIndex {
        entry,
        mut reservation,
    } = pending;
    let mut cache = reservation
        .cache
        .lock()
        .map_err(|_| "vector cache unavailable")?;
    cache.reserved_bytes -= std::mem::take(&mut reservation.bytes);
    if cache
        .entries
        .iter()
        .any(|old| old.partition == entry.partition && old.epoch > entry.epoch)
    {
        drop(entry);
        return Ok(());
    }
    cache.entries.retain(|old| old.partition != entry.partition);
    ensure(
        cache.make_room(entry.resident_bytes(), cache_budget()?),
        "vector index warming",
    )?;
    while cache.entries.len() >= 16 {
        cache.entries.pop_front();
    }
    cache.entries.push_back(entry);
    Ok(())
}
fn check_build_running() -> Result<()> {
    ensure(
        !WARMER
            .get()
            .is_some_and(|w| w.stopped.load(Ordering::Relaxed)),
        "vector index build cancelled",
    )
}
fn build_index(
    k: &Knowledge,
    model: &str,
    dim: usize,
    partition: String,
    key: String,
    generation: (i64, i64),
    release_snapshot: bool,
) -> Result<PendingIndex> {
    // Serialize construction, but do not retain this guard in PendingIndex:
    // publication may be delayed while a newer generation is constructed.
    let _builder = BUILD_LOCK
        .lock()
        .map_err(|_| "vector builder unavailable")?;
    check_build_running()?;
    let started = std::time::Instant::now();
    let snapshot = if k.db.conn.is_autocommit() {
        Some(k.db.conn.unchecked_transaction()?)
    } else {
        None
    };
    let budget = cache_budget()?;
    let capacity = vector_capacity(dim, budget);
    let count: i64 = k.db.conn.query_row(
        INDEX_COUNT_SQL,
        params![k.session.scope, (capacity + 1) as i64],
        |r| r.get(0),
    )?;
    let count = usize::try_from(count).map_err(|_| "invalid vector index count")?;
    ensure(count <= capacity, "vector index capacity")?;
    let reservation =
        BuildReservation::acquire(index_cache().clone(), build_bytes(dim, count), budget)?;
    let mut ids = BTreeMap::new();
    let mut vectors = Vec::with_capacity(count * dim);
    let mut external = Vec::with_capacity(count);
    {
        let mut statement = k.db.conn.prepare_cached(INDEX_ROWS_SQL)?;
        let mut rows = statement.query(params![k.session.scope, (capacity + 1) as i64])?;
        while let Some(row) = rows.next()? {
            if external.len() % BUILD_BATCH_ROWS == 0 {
                check_build_running()?;
            }
            ensure(external.len() < count, "vector index snapshot changed")?;
            let pk: i64 = row.get(0)?;
            ensure(pk > 0, "invalid stored memory identity")?;
            let id: Vec<u8> = row.get(1)?;
            let raw: Vec<u8> = row.get(2)?;
            let checksum: Vec<u8> = row.get(3)?;
            vectors.extend(decode_vector(&raw, &checksum, dim)?);
            ids.insert(
                pk as u64,
                id.try_into()
                    .map_err(|_| "invalid stored memory identity")?,
            );
            external.push(pk as u64);
        }
    }
    // The normal host's expensive quantisation runs after releasing its read snapshot.
    ensure(external.len() == count, "vector index snapshot changed")?;
    if let Some(snapshot) = snapshot {
        snapshot.commit()?;
    } else if release_snapshot {
        k.db.conn.execute_batch("COMMIT")?;
    }
    let copy_ms = started.elapsed().as_secs_f64() * 1000.0;
    let quantising = std::time::Instant::now();
    let mut index =
        turbovec::IdMapIndex::new(dim, 4).map_err(|_| "vector index construction failed")?;
    for (batch, keys) in vectors
        .chunks(BUILD_BATCH_ROWS * dim)
        .zip(external.chunks(BUILD_BATCH_ROWS))
    {
        check_build_running()?;
        index
            .add_with_ids(batch, keys)
            .map_err(|_| "vector index build failed")?;
    }
    drop(vectors);
    Ok(PendingIndex {
        entry: CachedIndex {
            partition,
            key,
            index,
            reverse: ids.iter().map(|(pk, id)| (*id, *pk)).collect(),
            ids,
            retained_rows: count,
            model: model.into(),
            dim,
            cursor: generation.0,
            epoch: generation.1,
            build: json!({"rows":count,"copy_ms":copy_ms,"quantise_ms":quantising.elapsed().as_secs_f64()*1000.0,
            "estimated_resident_bytes":resident_bytes(dim,count),"reserved_build_bytes":reservation.bytes,
            "quantise_batch_rows":BUILD_BATCH_ROWS}),
        },
        reservation,
    })
}
fn refresh_cached(
    k: &Knowledge,
    model: &str,
    dim: usize,
    epoch: i64,
    partition: &str,
    key: &str,
    cache: &mut IndexCache,
) -> Result<Option<(bool, usize)>> {
    let position = cache.entries.iter().position(|c| c.key == key);
    let hit = position.is_some();
    let mut reused = hit;
    let mut updated = 0;
    if let Some(position) = position {
        let entry = cache.entries.remove(position).unwrap();
        cache.entries.push_back(entry);
    }
    let (first, last): (i64, i64) = k.db.conn.query_row(
        "SELECT coalesce(min(seq),0),coalesce(max(seq),0) FROM native_vector_change",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    if !hit {
        let previous = cache.entries.iter().position(|c| {
            c.partition == partition
                && c.model == model
                && c.dim == dim
                && c.epoch <= epoch
                && c.cursor >= first - 1
                && c.cursor <= last
        });
        if let Some(position) = previous {
            let mut entry = cache.entries.remove(position).unwrap();
            let mut statement = k.db.conn.prepare_cached("SELECT DISTINCT memory_id FROM native_vector_change WHERE scope=? AND seq>? LIMIT 513")?;
            let changed = statement
                .query_map(params![k.session.scope, entry.cursor], |r| {
                    r.get::<_, Vec<u8>>(0)
                })?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            let budget = cache_budget()?;
            let retained_rows = entry.retained_rows.max(entry.ids.len() + changed.len());
            if changed.len() <= 512 && cache.make_room(resident_bytes(dim, retained_rows), budget) {
                for id in &changed {
                    let external: [u8; 16] = id
                        .as_slice()
                        .try_into()
                        .map_err(|_| "invalid stored memory identity")?;
                    if let Some(pk) = entry.reverse.remove(&external) {
                        entry.index.remove(pk);
                        entry.ids.remove(&pk);
                    }
                    let row: Option<(i64,Vec<u8>,Vec<u8>)> = k.db.conn.query_row("SELECT m.memory_pk,v.vector,v.vector_checksum FROM native_vector v JOIN cortex_memory m INDEXED BY native_memory_scope_id ON m.memory_id=v.memory_id WHERE v.memory_id=? AND v.scope=? AND m.scope=v.scope AND m.status=0 AND m.record_checksum=v.checksum",params![id,k.session.scope],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
                    if let Some((pk, raw, checksum)) = row {
                        ensure(
                            pk > 0 && entry.ids.len() < vector_capacity(dim, cache_budget()?),
                            "vector index capacity",
                        )?;
                        let vector = decode_vector(&raw, &checksum, dim)?;
                        entry
                            .index
                            .add_with_ids(&vector, &[pk as u64])
                            .map_err(|_| "vector update failed")?;
                        entry.ids.insert(pk as u64, external);
                        entry.reverse.insert(external, pk as u64);
                        entry.retained_rows = entry.retained_rows.max(entry.ids.len());
                    }
                }
                updated = changed.len();
                entry.key = key.into();
                entry.cursor = last;
                entry.epoch = epoch;
                cache.entries.push_back(entry);
                reused = true;
            }
        }
    }
    Ok(reused.then_some((hit, updated)))
}
fn candidates(
    k: &Knowledge,
    model: &str,
    dim: usize,
    epoch: i64,
    query: &[f32],
) -> Result<(Vec<String>, bool, usize, Value)> {
    let (partition, key, cursor) = index_identity(k, model, dim, epoch)?;
    let mut cache = lock_cache()?;
    let refreshed = refresh_cached(k, model, dim, epoch, &partition, &key, &mut cache)?;
    let (hit, updated) = refreshed.unwrap_or((false, 0));
    if refreshed.is_none() {
        if let Some(warmer) = WARMER.get() {
            drop(cache);
            if let Ok(errors) = warmer.failed.lock()
                && errors
                    .get(&k.session.scope)
                    .is_some_and(|(failed_key, capacity)| failed_key == &key && *capacity)
            {
                return Err("vector index capacity".into());
            }
            let mut pending = warmer
                .pending
                .lock()
                .map_err(|_| "vector warmer unavailable")?;
            if pending.insert(k.session.scope.clone())
                && warmer.sender.try_send(k.session.clone()).is_err()
            {
                pending.remove(&k.session.scope);
            }
            return Err("vector index warming".into());
        }
        // One-shot administrative commands retain synchronous behaviour.
        drop(cache);
        publish(build_index(
            k,
            model,
            dim,
            partition,
            key.clone(),
            (cursor, epoch),
            false,
        )?)?;
        cache = lock_cache()?;
    }
    let cache = cache
        .entries
        .iter()
        .find(|entry| entry.key == key)
        .ok_or("vector index warming")?;
    if cache.ids.is_empty() {
        return Ok((Vec::new(), hit, updated, cache.build.clone()));
    }
    let (_, ids) = cache.index.search(query, 64.min(cache.ids.len()));
    Ok((
        ids.iter()
            .filter_map(|id| cache.ids.get(id).map(hex::encode))
            .collect(),
        hit,
        updated,
        cache.build.clone(),
    ))
}
fn decode_vector(raw: &[u8], checksum: &[u8], dim: usize) -> Result<Vec<f32>> {
    ensure(
        raw.len() == dim * 4 && sha(raw) == checksum,
        "stored vector integrity failure",
    )?;
    let vector: Vec<f32> = raw
        .as_chunks::<4>()
        .0
        .iter()
        .map(|chunk| f32::from_le_bytes(*chunk))
        .collect();
    ensure(
        vector.iter().all(|v| v.is_finite()),
        "invalid stored vector",
    )?;
    Ok(vector)
}
fn similarity(k: &Knowledge, id: &str, query: &[f32]) -> Result<Option<f64>> {
    let row: Option<(Vec<u8>,Vec<u8>)> = k.db.conn.prepare_cached(
        "SELECT v.vector,v.vector_checksum FROM native_vector v JOIN cortex_memory m INDEXED BY native_memory_scope_id ON m.memory_id=v.memory_id WHERE v.scope=? AND v.memory_id=? AND m.scope=v.scope AND m.status=0 AND m.record_checksum=v.checksum")?
        .query_row(params![k.session.scope,identifier(id)?],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    let Some((raw, checksum)) = row else {
        return Ok(None);
    };
    ensure(
        raw.len() == query.len() * 4 && sha(&raw) == checksum,
        "stored vector integrity failure",
    )?;
    let mut dot = 0.0_f64;
    let mut norm = 0.0_f64;
    let mut query_norm = 0.0_f64;
    for (chunk, q) in raw.as_chunks::<4>().0.iter().zip(query) {
        let v = f32::from_le_bytes(*chunk) as f64;
        ensure(v.is_finite(), "stored vector invalid")?;
        dot += v * (*q as f64);
        norm += v * v;
        query_norm += (*q as f64) * (*q as f64);
    }
    ensure(norm > 1e-20 && query_norm > 1e-20, "invalid vector norm")?;
    Ok(Some((dot / (norm * query_norm).sqrt()).clamp(-1.0, 1.0)))
}
fn recall(k: &Knowledge, a: &Value, shortlist: bool) -> Result<Value> {
    keys(a, &["query"], &["model", "vector"])?;
    let original_query = field(a, "query")?;
    policy::check_text(original_query, 4096)?;
    let query = super::answerability::search_query(original_query);
    let started = std::time::Instant::now();
    let (lexical_ids, lexical_capped) = if shortlist {
        k.lexical_ids(query, 64)?
    } else {
        let lexical = k.recall(query, "lexical", 8, 1400)?;
        (
            lexical["memories"]
                .as_array()
                .unwrap()
                .iter()
                .map(|item| field(item, "id").map(str::to_owned))
                .collect::<Result<Vec<_>>>()?,
            lexical["candidates_capped"] == true,
        )
    };
    let mut result = json!({"scope":k.session.scope,"mode":"vector_lexical","memories":[],"source_checked":true,"vector_state":"unavailable","lexical_candidates_capped":lexical_capped,"data_only":true,
        "tracking_label_projected":query!=original_query});
    let mut stages = json!({"lexical_ms":started.elapsed().as_secs_f64()*1000.0});
    let mut scores = BTreeMap::<String, f64>::new();
    let mut similarities = BTreeMap::new();
    let mut preview_freshness = BTreeMap::new();
    let mut answerability_filtered = 0;
    for (rank, id) in lexical_ids.iter().enumerate() {
        scores.insert(id.clone(), 1.0 / (60 + rank) as f64);
    }
    if let Some((model, dim, epoch)) = configured(k)? {
        let pending: i64 = k.db.conn.query_row(
            "SELECT count(*) FROM native_vector_job WHERE scope=?",
            [&k.session.scope],
            |r| r.get(0),
        )?;
        result["pending_index_updates"] = json!(pending);
        if a["model"] == model {
            let vector = normalise(&a["vector"], dim)?;
            let candidate_started = std::time::Instant::now();
            let candidate_result = candidates(k, &model, dim, epoch, &vector);
            stages["index_search_ms"] = json!(candidate_started.elapsed().as_secs_f64() * 1000.0);
            match candidate_result {
                Ok((ids, hit, updated, build)) => {
                    result["vector_state"] = json!(if pending == 0 { "ready" } else { "lagging" });
                    result["index_cache_hit"] = json!(hit);
                    result["index_delta_updates"] = json!(updated);
                    result["index_build"] = build;
                    let eligible_ids: Vec<_> = ids
                        .iter()
                        .chain(scores.keys())
                        .cloned()
                        .collect::<std::collections::BTreeSet<_>>()
                        .into_iter()
                        .collect();
                    let rescore_started = std::time::Instant::now();
                    let raw_scores = eligible_ids
                        .iter()
                        .map(|id| similarity(k, id, &vector).map(|score| (id.clone(), score)))
                        .collect::<Result<BTreeMap<_, _>>>()?;
                    stages["exact_rescore_ms"] =
                        json!(rescore_started.elapsed().as_secs_f64() * 1000.0);
                    let freshness_started = std::time::Instant::now();
                    let plausible_ids: Vec<_> = eligible_ids
                        .into_iter()
                        .filter(|id| raw_scores[id].is_none_or(|score| score >= MIN_SIMILARITY))
                        .collect();
                    let sources = k.source_items(&plausible_ids)?;
                    let source_reader = super::knowledge::SourceReader::new(k.session);
                    let mut source_cache = BTreeMap::new();
                    let mut inspected = BTreeSet::new();
                    for id in ids.iter().chain(scores.keys()) {
                        if inspected.insert(id) {
                            let similarity = raw_scores[id];
                            if similarity.is_some_and(|score| score < MIN_SIMILARITY) {
                                similarities.insert(id.clone(), similarity);
                                continue;
                            }
                            let Ok((_, payload)) = k.db.memory(&k.session.scope, id, true) else {
                                continue;
                            };
                            if shortlist
                                && !super::answerability::supports_requested_field(
                                    query,
                                    &format!("{}\n{}", payload[0], payload[1]),
                                )
                            {
                                answerability_filtered += 1;
                                continue;
                            }
                            let freshness =
                                k.freshness_with(id, &sources, &source_reader, &mut source_cache)?;
                            if freshness["state"] == "fresh" {
                                similarities.insert(id.clone(), similarity);
                                if shortlist {
                                    preview_freshness.insert(id.clone(), freshness);
                                }
                            }
                        }
                    }
                    result["candidate_source_checks"] = json!(source_cache.len());
                    let best = similarities
                        .values()
                        .filter_map(|v| *v)
                        .fold(-1.0_f64, f64::max);
                    stages["candidate_freshness_ms"] =
                        json!(freshness_started.elapsed().as_secs_f64() * 1000.0);
                    let floor = if shortlist {
                        MIN_SIMILARITY
                    } else {
                        MIN_SIMILARITY.max(best - BEST_MATCH_BAND)
                    };
                    result["relevance_floor"] = json!(floor);
                    result["best_similarity"] = json!(best);
                    let mut exact: Vec<_> = ids
                        .iter()
                        .filter_map(|id| {
                            similarities
                                .get(id)
                                .and_then(|s| *s)
                                .map(|score| (id, score))
                        })
                        .filter(|(_, score)| *score >= floor)
                        .collect();
                    exact.sort_by(|a, b| b.1.total_cmp(&a.1).then_with(|| a.0.cmp(b.0)));
                    // Source-bound lexical hits without an indexed vector remain available during lag.
                    scores.retain(|id, _| {
                        similarities
                            .get(id)
                            .is_some_and(|s| s.is_none_or(|score| score >= floor))
                    });
                    result["relevance_filtered"] = json!(
                        similarities
                            .values()
                            .filter(|v| v.is_some_and(|score| score < floor))
                            .count()
                    );
                    for (rank, (id, _)) in exact.iter().enumerate() {
                        *scores.entry((*id).clone()).or_default() += 1.0 / (60 + rank) as f64;
                    }
                }
                Err(error) => {
                    result["vector_state"] =
                        json!(if error.to_string() == "vector index capacity" {
                            "capacity_limited"
                        } else if error.to_string() == "vector index warming" {
                            "warming"
                        } else {
                            "rebuild_unavailable_lexical_fallback"
                        });
                    result["index_cache_hit"] = json!(false);
                }
            }
        }
    }
    let mut ranked: Vec<_> = scores.into_iter().collect();
    let packet_started = std::time::Instant::now();
    ranked.sort_by(|a, b| b.1.total_cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    let mut memories = Vec::new();
    let mut excluded = 0;
    let mut preview_freshness_reused = 0;
    let limit = if shortlist && super::answerability::multiple_topics(query) {
        16
    } else {
        8
    };
    let mut candidates_capped = false;
    for (id, score) in ranked {
        let Ok((row, p)) = k.db.memory(&k.session.scope, &id, true) else {
            excluded += 1;
            continue;
        };
        if shortlist
            && !super::answerability::supports_requested_field(
                query,
                &format!("{}\n{}", p[0], p[1]),
            )
        {
            answerability_filtered += 1;
            continue;
        }
        // This map lives only within the read-only preview. Final selection in
        // the subsequent request still revalidates sources and record versions.
        let freshness = match preview_freshness.remove(&id) {
            Some(freshness) => {
                preview_freshness_reused += 1;
                freshness
            }
            None => k.freshness(&id)?,
        };
        if freshness["state"] != "fresh" {
            excluded += 1;
            continue;
        }
        if memories.len() == limit {
            candidates_capped = true;
            break;
        }
        let mut item = json!({"id":id,"subject":p[0],"summary":p[1],"source":p[4],"source_hash":row.value("source_hash")?,"confidence":row.int("confidence")? as f64/255.0,"freshness":freshness,"rrf_score":score});
        if shortlist {
            item["similarity"] = json!(similarities.get(&id).and_then(|s| *s));
        }
        memories.push(item);
        result["memories"] = json!(memories);
        if !shortlist && tokens(&canonical(&result, false)?) > 1300 {
            memories.pop();
        }
    }
    result["memories"] = json!(memories);
    result["excluded_inactive_or_unfresh"] = json!(excluded);
    result["answerability_filtered"] = json!(answerability_filtered);
    result["preview_freshness_reused"] = json!(preview_freshness_reused);
    result["rerank_shortlist_capped"] = json!(shortlist && candidates_capped);
    result["observed_at"] = json!(now());
    stages["packet_freshness_ms"] = json!(packet_started.elapsed().as_secs_f64() * 1000.0);
    result["retrieval_stages_ms"] = stages;
    ensure(
        shortlist || tokens(&canonical(&result, false)?) <= 1400,
        "vector response token budget",
    )?;
    Ok(result)
}

#[cfg(test)]
mod scheduling_tests {
    use super::CLAIM_JOBS_SQL;
    use rusqlite::{Connection, params};

    #[test]
    fn claims_follow_enqueue_order_and_skip_owned_or_quarantined_jobs() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("CREATE TABLE native_vector_job(memory_id BLOB PRIMARY KEY,scope TEXT NOT NULL);
            CREATE INDEX native_vector_job_fifo ON native_vector_job(scope);
            CREATE TABLE native_vector_lease(memory_id BLOB PRIMARY KEY,expires_ms INTEGER,attempts INTEGER);").unwrap();
        for id in [9_u8, 2, 7, 1] {
            conn.execute("INSERT INTO native_vector_job VALUES(?,'a')", [vec![id]])
                .unwrap();
        }
        conn.execute(
            "INSERT INTO native_vector_job VALUES(?,'b')",
            [vec![255_u8]],
        )
        .unwrap();
        let fetch = || {
            conn.prepare(CLAIM_JOBS_SQL)
                .unwrap()
                .query_map(params!["a", 100], |r| r.get::<_, Vec<u8>>(0))
                .unwrap()
                .map(Result::unwrap)
                .collect::<Vec<_>>()
        };
        assert_eq!(fetch(), vec![vec![9], vec![2], vec![7], vec![1]]);
        conn.execute(
            "INSERT INTO native_vector_lease VALUES(?,200,1)",
            [vec![9_u8]],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO native_vector_lease VALUES(?,0,5)",
            [vec![2_u8]],
        )
        .unwrap();
        conn.execute("INSERT INTO native_vector_job VALUES(?,'a')", [vec![0_u8]])
            .unwrap();
        assert_eq!(fetch(), vec![vec![7], vec![1], vec![0]]);
        conn.execute(
            "UPDATE native_vector_lease SET expires_ms=0 WHERE memory_id=?",
            [vec![9_u8]],
        )
        .unwrap();
        assert_eq!(fetch(), vec![vec![9], vec![7], vec![1], vec![0]]);
    }
}
