//! Durable archive policy. Cleanup is host maintenance, never model generation.
use super::{
    Result,
    database::{Database, field},
    ensure,
    knowledge::{Knowledge, keys},
    sha,
};
use crate::{Request, Session};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};

const BATCH: i64 = 32;
pub const DEFAULT_DAYS: i64 = 365;

pub fn initialise(db: &Database) -> Result<()> {
    db.conn.execute_batch("CREATE TABLE IF NOT EXISTS native_archive_policy(scope TEXT PRIMARY KEY,days INTEGER CHECK(days IS NULL OR days BETWEEN 1 AND 36500));
        CREATE TABLE IF NOT EXISTS native_archive_chat(scope TEXT NOT NULL,chat_id TEXT NOT NULL,archived_at INTEGER,cleaned INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(scope,chat_id));
        CREATE INDEX IF NOT EXISTS native_archive_chat_due ON native_archive_chat(scope,cleaned,archived_at);
        CREATE TABLE IF NOT EXISTS native_project(scope TEXT PRIMARY KEY,state TEXT NOT NULL,version INTEGER NOT NULL,archived_at INTEGER,cleaned INTEGER NOT NULL DEFAULT 0);")?;
    // Old archives have no trustworthy archival date: start a full grace period now.
    db.conn.execute("INSERT OR IGNORE INTO native_archive_chat(scope,chat_id,archived_at) SELECT scope,chat_id,? FROM native_chat WHERE state IN ('archived','deleted')", [chrono::Utc::now().timestamp()])?;
    Ok(())
}

fn days(k: &Knowledge) -> Result<Option<i64>> {
    Ok(k.db
        .conn
        .query_row(
            "SELECT days FROM native_archive_policy WHERE scope=?",
            [&k.session.scope],
            |r| r.get::<_, Option<i64>>(0),
        )
        .optional()?
        .unwrap_or(Some(DEFAULT_DAYS)))
}

pub fn chat_transition(k: &Knowledge, chat: &str, state: &str) -> Result<()> {
    if state == "active" {
        k.db.conn.execute(
            "DELETE FROM native_archive_chat WHERE scope=? AND chat_id=?",
            params![k.session.scope, chat],
        )?;
    } else if state == "archived" || state == "deleted" {
        k.db.conn.execute(
            "INSERT OR IGNORE INTO native_archive_chat(scope,chat_id,archived_at) VALUES(?,?,?)",
            params![k.session.scope, chat, chrono::Utc::now().timestamp()],
        )?;
    }
    Ok(())
}

pub fn check_source(k: &Knowledge, path: &str) -> Result<()> {
    let state: Option<String> = k.db.conn.query_row("SELECT c.state FROM native_chat_source s JOIN native_chat c USING(scope,chat_id) WHERE s.scope=? AND s.path_hash=?",params![k.session.scope,sha(path.as_bytes())],|r|r.get(0)).optional()?;
    ensure(
        state.is_none_or(|s| s == "active"),
        "source chat is not active",
    )
}

pub fn check_request(db: &Database, s: &Session, r: &Request) -> Result<()> {
    let exists: bool = db.conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='native_project')",
        [],
        |r| r.get(0),
    )?;
    if !exists {
        return Ok(());
    }
    if r.operation == "routing-export"
        || (r.operation == "admin" && r.arguments["action"] == "export")
    {
        let controls:bool=db.conn.query_row("SELECT EXISTS(SELECT 1 FROM native_project WHERE scope=?) OR EXISTS(SELECT 1 FROM native_archive_policy WHERE scope=?)",params![s.scope,s.scope],|r|r.get(0))?;
        ensure(
            !controls,
            "legacy export cannot preserve project lifecycle or retention policy; use a complete vault backup",
        )?;
    }
    let state: Option<String> = db
        .conn
        .query_row(
            "SELECT state FROM native_project WHERE scope=?",
            [&s.scope],
            |r| r.get(0),
        )
        .optional()?;
    let control = r.operation == "admin"
        && matches!(
            r.arguments["action"].as_str(),
            Some(
                "project-event"
                    | "archive-retention"
                    | "archive-retention-status"
                    | "archive-cleanup"
                    | "verify"
            )
        );
    ensure(
        state.is_none_or(|v| v == "active")
            || control
            || matches!(r.operation.as_str(), "ping" | "catalogue"),
        "project is not active",
    )?;
    if r.operation == "admin" && r.arguments["action"] == "import" {
        let captured: bool = db.conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM native_chat WHERE scope=?)",
            [&s.scope],
            |r| r.get(0),
        )?;
        ensure(!captured, "legacy import cannot preserve chat lifecycle")?;
    }
    Ok(())
}

fn purge(k: &Knowledge, id: &[u8]) -> Result<()> {
    super::admin::execute(
        k,
        &json!({"action":"purge","arguments":{"memory_id":hex::encode(id),"user_confirmed":true}}),
    )?;
    Ok(())
}

pub fn purge_linked_exact(k: &Knowledge, id: &[u8]) -> Result<()> {
    let ids =
        k.db.conn
            .prepare("SELECT archive_id FROM cortex_verbatim WHERE scope=? AND linked_memory_id=?")?
            .query_map(params![k.session.scope, id], |r| r.get::<_, Vec<u8>>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
    for id in ids {
        purge(k, &id)?;
    }
    Ok(())
}

fn delete_chat_aux(k: &Knowledge, chat: &str) -> Result<usize> {
    let mut removed = 0;
    let copies=k.db.conn.prepare("SELECT v.archive_id FROM cortex_verbatim v WHERE v.scope=? AND EXISTS(SELECT 1 FROM native_chat_link l WHERE l.scope=v.scope AND l.chat_id=? AND l.memory_id=v.linked_memory_id) AND NOT EXISTS(SELECT 1 FROM knowledge_item i WHERE i.scope=v.scope AND i.owner=v.linked_memory_id AND i.kind='source' AND NOT EXISTS(SELECT 1 FROM native_chat_path p WHERE p.scope=i.scope AND p.chat_id=? AND p.path=json_extract(i.payload,'$.path'))) LIMIT ?")?.query_map(params![k.session.scope,chat,chat,BATCH],|r|r.get::<_,Vec<u8>>(0))?.collect::<rusqlite::Result<Vec<_>>>()?;
    removed += copies.len();
    for id in copies {
        purge(k, &id)?;
    }
    let stages=k.db.conn.prepare("SELECT s.stage_id FROM hippocampus_stage s WHERE s.scope=? AND EXISTS(SELECT 1 FROM cortex_memory m JOIN native_chat_link l ON l.memory_id=m.memory_id AND l.scope=m.scope WHERE m.stage_id=s.stage_id AND m.scope=s.scope AND l.chat_id=?) AND NOT EXISTS(SELECT 1 FROM cortex_memory m WHERE m.scope=s.scope AND m.stage_id=s.stage_id AND (NOT EXISTS(SELECT 1 FROM native_chat_link l WHERE l.scope=m.scope AND l.memory_id=m.memory_id AND l.chat_id=?) OR EXISTS(SELECT 1 FROM knowledge_item i WHERE i.scope=m.scope AND i.owner=m.memory_id AND i.kind='source' AND NOT EXISTS(SELECT 1 FROM native_chat_path p WHERE p.scope=i.scope AND p.chat_id=? AND p.path=json_extract(i.payload,'$.path'))))) LIMIT ?")?.query_map(params![k.session.scope,chat,chat,chat,BATCH],|r|r.get::<_,Vec<u8>>(0))?.collect::<rusqlite::Result<Vec<_>>>()?;
    removed += stages.len();
    for id in stages {
        purge(k, &id)?;
    }
    for (table, id) in [
        ("cortex_verbatim", "archive_id"),
        ("hippocampus_stage", "stage_id"),
    ] {
        let ids=k.db.conn.prepare(&format!("SELECT {id} FROM {table} r JOIN native_chat_path p ON p.scope=r.scope AND p.path=r.source WHERE r.scope=? AND p.chat_id=? LIMIT ?"))?.query_map(params![k.session.scope,chat,BATCH],|r|r.get::<_,Vec<u8>>(0))?.collect::<rusqlite::Result<Vec<_>>>()?;
        removed += ids.len();
        for id in ids {
            purge(k, &id)?;
        }
    }
    removed+=k.db.conn.execute("DELETE FROM knowledge_item WHERE id IN (SELECT i.id FROM knowledge_item i JOIN native_chat_path p ON p.scope=i.scope AND p.path=json_extract(i.payload,'$.binding.path') WHERE i.scope=? AND p.chat_id=? AND i.kind='proposal' LIMIT ?)",params![k.session.scope,chat,BATCH])?;
    Ok(removed)
}

fn project_cleanup(k: &Knowledge) -> Result<Value> {
    let mut removed = 0;
    // Exact records go first so a memory purge cannot orphan their ownership.
    for (table, key) in [
        ("cortex_verbatim", "archive_id"),
        ("hippocampus_stage", "stage_id"),
        ("cortex_memory", "memory_id"),
    ] {
        let ids =
            k.db.conn
                .prepare(&format!("SELECT {key} FROM {table} WHERE scope=? LIMIT ?"))?
                .query_map(params![k.session.scope, BATCH], |r| r.get::<_, Vec<u8>>(0))?
                .collect::<rusqlite::Result<Vec<_>>>()?;
        removed += ids.len();
        for id in ids {
            purge(k, &id)?;
        }
        if removed > 0 {
            return Ok(json!({"removed":removed,"pending":true,"project":true}));
        }
    }
    for table in [
        "knowledge_item",
        "session_route",
        "native_chat_path",
        "native_chat_link",
    ] {
        removed+=k.db.conn.execute(&format!("DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE scope=? LIMIT ?)"),params![k.session.scope,BATCH])?;
    }
    if removed == 0 {
        let vectors: bool = k.db.conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='native_vector_config')",
            [],
            |r| r.get(0),
        )?;
        if vectors {
            for table in ["native_vector_change", "native_vector_config"] {
                k.db.conn.execute(
                    &format!("DELETE FROM {table} WHERE scope=?"),
                    [&k.session.scope],
                )?;
            }
        }
        k.db.conn.execute(
            "UPDATE native_chat SET state='deleted' WHERE scope=?",
            [&k.session.scope],
        )?;
        k.db.conn.execute(
            "UPDATE native_archive_chat SET cleaned=1 WHERE scope=?",
            [&k.session.scope],
        )?;
        k.db.conn.execute(
            "UPDATE native_project SET cleaned=1 WHERE scope=?",
            [&k.session.scope],
        )?;
    }
    super::recovery::revoke_scope(k.db, &k.session.scope)?;
    Ok(json!({"removed":removed,"pending":removed>0,"project":true}))
}

fn sweep(k: &Knowledge, clock: i64) -> Result<Value> {
    let cutoff = days(k)?.map(|d| clock - d * 86400);
    let project: Option<(String, Option<i64>, bool)> =
        k.db.conn
            .query_row(
                "SELECT state,archived_at,cleaned FROM native_project WHERE scope=?",
                [&k.session.scope],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()?;
    if let Some((state, archived, cleaned)) = &project
        && !cleaned
        && (state == "deleted"
            || (state == "archived" && cutoff.zip(*archived).is_some_and(|(c, a)| a <= c)))
    {
        k.db.conn.execute(
            "UPDATE native_project SET state='deleted' WHERE scope=?",
            [&k.session.scope],
        )?;
        let mut result = project_cleanup(k)?;
        result["project_state"] = json!("deleted");
        return Ok(result);
    }
    let chat:Option<(String,i64,String)>=k.db.conn.query_row("SELECT c.chat_id,c.version,c.state FROM native_archive_chat a JOIN native_chat c USING(scope,chat_id) WHERE a.scope=? AND a.cleaned=0 AND (c.state='deleted' OR (c.state='archived' AND a.archived_at<=?)) ORDER BY a.archived_at,c.chat_id LIMIT 1",params![k.session.scope,cutoff],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
    let Some((chat, version, _)) = chat else {
        return Ok(
            json!({"removed":0,"pending":false,"project_state":project.map(|p|p.0).unwrap_or_else(||"active".into())}),
        );
    };
    // Local expiry must not invent a newer version of an upstream lifecycle event.
    k.db.conn.execute(
        "UPDATE native_chat SET state='deleted' WHERE scope=? AND chat_id=?",
        params![k.session.scope, chat],
    )?;
    let auxiliary = delete_chat_aux(k, &chat)?;
    let result = super::chat_lifecycle::execute(
        k,
        "chat-drain",
        &json!({"chat_id":chat,"version":version,"state":"deleted"}),
    )?;
    let links: bool = k.db.conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM native_chat_link WHERE scope=? AND chat_id=?)",
        params![k.session.scope, chat],
        |r| r.get(0),
    )?;
    let pending = auxiliary > 0 || links;
    if !pending {
        k.db.conn.execute(
            "DELETE FROM native_chat_path WHERE scope=? AND chat_id=?",
            params![k.session.scope, chat],
        )?;
        k.db.conn.execute(
            "UPDATE native_archive_chat SET cleaned=1 WHERE scope=? AND chat_id=?",
            params![k.session.scope, chat],
        )?;
    }
    // Routes do not have reliable chat ownership; invalidate this scope's derived routing cache.
    k.db.conn.execute(
        "DELETE FROM session_route WHERE scope=?",
        [&k.session.scope],
    )?;
    Ok(
        json!({"removed":auxiliary+result["purged"].as_u64().unwrap_or(0) as usize,"pending":true,"chat_complete":!pending}),
    )
}

pub fn execute(k: &Knowledge, action: &str, a: &Value) -> Result<Value> {
    match action {
        "archive-retention" => {
            keys(a, &["days"], &[])?;
            let days = if a["days"].is_null() {
                None
            } else {
                Some(
                    a["days"]
                        .as_i64()
                        .ok_or("retention days must be an integer or null")?,
                )
            };
            ensure(
                days.is_none_or(|d| (1..=36500).contains(&d)),
                "retention days out of range",
            )?;
            k.db.conn.execute("INSERT INTO native_archive_policy VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET days=excluded.days",params![k.session.scope,days])?;
            Ok(json!({"days":days,"enabled":days.is_some(),"applies_to_existing_archives":true}))
        }
        "archive-retention-status" => {
            keys(a, &[], &[])?;
            let days = days(k)?;
            let chats: i64 = k.db.conn.query_row(
                "SELECT count(*) FROM native_archive_chat WHERE scope=? AND cleaned=0",
                [&k.session.scope],
                |r| r.get(0),
            )?;
            let project: Option<(String, Option<i64>, bool)> =
                k.db.conn
                    .query_row(
                        "SELECT state,archived_at,cleaned FROM native_project WHERE scope=?",
                        [&k.session.scope],
                        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                    )
                    .optional()?;
            Ok(
                json!({"days":days,"enabled":days.is_some(),"pending_chats":chats,"project":project,"backup_erasure":false}),
            )
        }
        "archive-cleanup" => {
            keys(a, &[], &[])?;
            sweep(k, chrono::Utc::now().timestamp())
        }
        "project-event" => {
            keys(a, &["state", "version"], &[])?;
            let state = field(a, "state")?;
            let version = a["version"].as_i64().ok_or("project version required")?;
            ensure(
                version > 0 && ["active", "archived", "deleted", "unknown"].contains(&state),
                "invalid project event",
            )?;
            let previous: Option<(String, i64, Option<i64>)> =
                k.db.conn
                    .query_row(
                        "SELECT state,version,archived_at FROM native_project WHERE scope=?",
                        [&k.session.scope],
                        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                    )
                    .optional()?;
            if let Some((old, seen, _)) = &previous {
                if version < *seen {
                    return Ok(json!({"applied":false,"stale":true}));
                }
                if version == *seen {
                    ensure(old == state, "project version collision")?;
                    return Ok(json!({"applied":false,"duplicate":true}));
                }
                ensure(
                    old != "deleted" || state == "deleted",
                    "deleted project cannot be resurrected",
                )?;
            }
            let archived = if state == "active" {
                None
            } else {
                previous
                    .and_then(|(_, _, a)| a)
                    .or_else(|| (state == "archived").then(|| chrono::Utc::now().timestamp()))
            };
            k.db.conn.execute("INSERT INTO native_project(scope,state,version,archived_at) VALUES(?,?,?,?) ON CONFLICT(scope) DO UPDATE SET state=excluded.state,version=excluded.version,archived_at=excluded.archived_at",params![k.session.scope,state,version,archived])?;
            super::recovery::revoke_scope(k.db, &k.session.scope)?;
            Ok(
                json!({"applied":true,"state":state,"version":version,"archived_at":archived,"scope":k.session.scope,"capture":"explicit_host_receipt","backup_erasure":false}),
            )
        }
        _ => Err("unsupported archive maintenance".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> (Database, Session) {
        let db = Database::initialize(rusqlite::Connection::open_in_memory().unwrap()).unwrap();
        Knowledge::initialize(&db).unwrap();
        let session=serde_json::from_value(json!({"id":"synthetic-chat","scope":"synthetic-project","source_root":"unused","allow_admin":true})).unwrap();
        (db, session)
    }
    fn event(k: &Knowledge, state: &str, version: i64) -> Value {
        super::super::chat_lifecycle::execute(
            k,
            "chat-event",
            &json!({"chat_id":"synthetic-chat","state":state,"version":version}),
        )
        .unwrap()
    }
    fn count(k: &Knowledge, table: &str) -> i64 {
        k.db.conn
            .query_row(
                &format!("SELECT count(*) FROM {table} WHERE scope=?"),
                [&k.session.scope],
                |r| r.get(0),
            )
            .unwrap()
    }
    fn archived_at(k: &Knowledge) -> i64 {
        k.db.conn
            .query_row(
                "SELECT archived_at FROM native_archive_chat WHERE scope=?",
                [&k.session.scope],
                |r| r.get(0),
            )
            .unwrap()
    }
    fn linked(k: &Knowledge, path: &str, subject: &str) -> Vec<u8> {
        let m=k.db.remember(&k.session.scope,&json!({"type":"semantic","subject":subject,"summary":"Synthetic release window is Thursday."})).unwrap();
        let id = m["memory_id"].as_str().unwrap();
        k.put(
            "source",
            json!({"path":path,"sha256":"0".repeat(64)}),
            Some(id),
            None,
        )
        .unwrap();
        super::super::chat_lifecycle::execute(
            k,
            "chat-link",
            &json!({"chat_id":"synthetic-chat","memory_id":id,"path":path}),
        )
        .unwrap();
        hex::decode(id).unwrap()
    }

    #[test]
    fn archive_deadline_policy_and_unarchive() {
        let (db, s) = fixture();
        let k = Knowledge::new(&db, &s).unwrap();
        assert_eq!(days(&k).unwrap(), Some(365));
        event(&k, "archived", 1);
        let start = archived_at(&k);
        event(&k, "archived", 1);
        event(&k, "archived", 2);
        assert_eq!(archived_at(&k), start);
        assert_eq!(
            sweep(&k, start + 365 * 86400 - 1).unwrap()["pending"],
            false
        );
        execute(&k, "archive-retention", &json!({"days":60})).unwrap();
        assert_eq!(sweep(&k, start + 30 * 86400).unwrap()["pending"], false);
        execute(&k, "archive-retention", &json!({"days":null})).unwrap();
        assert_eq!(sweep(&k, start + 100 * 86400).unwrap()["pending"], false);
        event(&k, "active", 3);
        assert_eq!(count(&k, "native_archive_chat"), 0);
        event(&k, "archived", 4);
        execute(&k, "archive-retention", &json!({"days":1})).unwrap();
        sweep(&k, archived_at(&k) + 86400).unwrap();
        assert!(
            super::super::chat_lifecycle::execute(
                &k,
                "chat-event",
                &json!({"chat_id":"synthetic-chat","state":"active","version":5})
            )
            .is_err()
        );
        for value in [
            json!(0),
            json!(-1),
            json!(36501),
            json!(1.5),
            json!("30"),
            json!(true),
        ] {
            assert!(execute(&k, "archive-retention", &json!({"days":value})).is_err());
        }
    }

    #[test]
    fn chat_cleanup_covers_copies_staging_proposals_and_preserves_independent_evidence() {
        let (db, s) = fixture();
        let k = Knowledge::new(&db, &s).unwrap();
        execute(&k, "archive-retention", &json!({"days":30})).unwrap();
        event(&k, "active", 1);
        let owned = linked(&k, "chat.md", "Owned release");
        let shared = linked(&k, "chat.md", "Shared release");
        k.put(
            "source",
            json!({"path":"independent.md","sha256":"0".repeat(64)}),
            Some(&hex::encode(&shared)),
            None,
        )
        .unwrap();
        super::super::admin::execute(&k,&json!({"action":"store-exact","arguments":{"text":"Synthetic exact copy","source":"chat.md","user_confirmed":true}})).unwrap();
        super::super::admin::execute(&k,&json!({"action":"store-exact","arguments":{"text":"Synthetic linked copy","linked_memory_id":hex::encode(&owned),"user_confirmed":true}})).unwrap();
        super::super::admin::execute(&k,&json!({"action":"stage","arguments":{"text":"Synthetic staged copy","source":"chat.md"}})).unwrap();
        // A pending proposal has no memory owner; source provenance must still remove it.
        db.conn.execute("INSERT INTO knowledge_item VALUES('synthetic-pending',?,'proposal',NULL,NULL,?, 'synthetic')",params![s.scope,json!({"binding":{"path":"chat.md"}}).to_string()]).unwrap();
        event(&k, "archived", 2);
        let clock = archived_at(&k) + 30 * 86400;
        for _ in 0..4 {
            sweep(&k, clock).unwrap();
        }
        assert_eq!(count(&k, "cortex_memory"), 1);
        assert_eq!(count(&k, "cortex_verbatim"), 0);
        assert_eq!(count(&k, "hippocampus_stage"), 0);
        assert_eq!(count(&k, "native_chat_link"), 0);
        assert_eq!(count(&k, "native_chat_path"), 0);
        super::super::admin::execute(&k, &json!({"action":"stats","arguments":{}})).unwrap();
        assert!(check_source(&k, "chat.md").is_err());
        assert!(check_source(&k, "independent.md").is_ok());
        db.verify_scope(&s.scope).unwrap();
        k.verify_items().unwrap();
        purge(&k, &shared).unwrap();
        super::super::admin::execute(&k, &json!({"action":"stats","arguments":{}})).unwrap();
    }

    #[test]
    fn chat_cleanup_batches_linked_copies_and_preserves_explicit_deletion_when_disabled() {
        let (db, s) = fixture();
        let k = Knowledge::new(&db, &s).unwrap();
        execute(&k, "archive-retention", &json!({"days":30})).unwrap();
        event(&k, "active", 1);
        let owned = linked(&k, "chat.md", "Owned fixture");
        for n in 0..40 {
            super::super::admin::execute(&k,&json!({"action":"store-exact","arguments":{"text":format!("Synthetic copy {n}"),"linked_memory_id":hex::encode(&owned),"user_confirmed":true}})).unwrap();
        }
        event(&k, "archived", 2);
        let clock = archived_at(&k) + 30 * 86400;
        sweep(&k, clock).unwrap();
        assert_eq!(count(&k, "cortex_verbatim"), 8);
        assert_eq!(count(&k, "cortex_memory"), 1);
        execute(&k, "archive-retention", &json!({"days":null})).unwrap();
        for _ in 0..4 {
            sweep(&k, clock).unwrap();
        }
        assert_eq!(count(&k, "cortex_verbatim"), 0);
        assert_eq!(count(&k, "cortex_memory"), 0);
        assert!(check_source(&k, "chat.md").is_err());
        db.verify_scope(&s.scope).unwrap();
    }

    #[test]
    fn project_cleanup_is_batched_isolated_and_blocks_replay() {
        let (db, s) = fixture();
        let k = Knowledge::new(&db, &s).unwrap();
        execute(&k, "archive-retention", &json!({"days":30})).unwrap();
        for n in 0..70 {
            db.remember(&s.scope,&json!({"type":"semantic","subject":format!("Synthetic {n}"),"summary":"Synthetic project fact."})).unwrap();
        }
        db.remember("independent-project",&json!({"type":"semantic","subject":"Independent","summary":"Keep this synthetic fact."})).unwrap();
        execute(
            &k,
            "project-event",
            &json!({"state":"archived","version":1}),
        )
        .unwrap();
        let start: i64 = db
            .conn
            .query_row("SELECT archived_at FROM native_project", [], |r| r.get(0))
            .unwrap();
        execute(
            &k,
            "project-event",
            &json!({"state":"archived","version":2}),
        )
        .unwrap();
        assert_eq!(sweep(&k, start + 30 * 86400 - 1).unwrap()["pending"], false);
        assert_eq!(sweep(&k, start + 30 * 86400).unwrap()["removed"], 32);
        assert_eq!(count(&k, "cortex_memory"), 38);
        // Reopening initialisation must not reset either deadlines or partial deletion.
        Knowledge::initialize(&db).unwrap();
        for _ in 0..8 {
            sweep(&k, start + 30 * 86400).unwrap();
        }
        assert_eq!(count(&k, "cortex_memory"), 0);
        assert_eq!(
            db.conn
                .query_row("SELECT count(*) FROM cortex_memory", [], |r| r
                    .get::<_, i64>(0))
                .unwrap(),
            1
        );
        assert!(execute(&k, "project-event", &json!({"state":"active","version":3})).is_err());
        let request:Request=serde_json::from_value(json!({"session":s.id,"id":"synthetic-request","operation":"admin","arguments":{"action":"remember","arguments":{}}})).unwrap();
        assert!(check_request(&db, &s, &request).is_err());
        db.verify_scope(&s.scope).unwrap();
    }

    #[test]
    fn empty_scope_stats_on_readonly_reopen() {
        let path = std::env::temp_dir().join(format!(
            "synthetic-retention-{}.sqlite3",
            uuid::Uuid::new_v4()
        ));
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            if cfg!(feature = "sqlcipher") {
                conn.execute_batch(
                    "PRAGMA cipher_log_level=ERROR; PRAGMA key='synthetic retention fixture';",
                )
                .unwrap();
            }
            let db = Database::initialize(conn).unwrap();
            Knowledge::initialize(&db).unwrap();
        }
        let (_, s) = fixture();
        let conn = rusqlite::Connection::open_with_flags(
            &path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        )
        .unwrap();
        if cfg!(feature = "sqlcipher") {
            conn.execute_batch(
                "PRAGMA cipher_log_level=ERROR; PRAGMA key='synthetic retention fixture';",
            )
            .unwrap();
        }
        let db = Database { conn };
        let r:Request=serde_json::from_value(json!({"session":s.id,"id":"synthetic-stats","operation":"admin","arguments":{"action":"stats","arguments":{}}})).unwrap();
        let result = super::super::service::respond(&db, &s, &r, true);
        drop(db);
        std::fs::remove_file(path).unwrap();
        result.unwrap();
    }

    #[test]
    fn legacy_archives_get_grace_period_and_unknown_waits_for_reconciliation() {
        let (db, s) = fixture();
        let k = Knowledge::new(&db, &s).unwrap();
        execute(&k, "archive-retention", &json!({"days":30})).unwrap();
        db.conn
            .execute(
                "INSERT INTO native_chat VALUES(?,'synthetic-chat','archived',1)",
                [&s.scope],
            )
            .unwrap();
        let before = chrono::Utc::now().timestamp();
        initialise(&db).unwrap();
        let start = archived_at(&k);
        assert!(start >= before);
        initialise(&db).unwrap();
        assert_eq!(archived_at(&k), start);
        event(&k, "unknown", 2);
        assert_eq!(sweep(&k, start + 31 * 86400).unwrap()["pending"], false);
        event(&k, "archived", 3);
        assert_eq!(archived_at(&k), start);
        assert_eq!(
            sweep(&k, start + 31 * 86400).unwrap()["chat_complete"],
            true
        );
    }
}
