//! Explicit host-owned lifecycle receipts. No native event capture is implied.
use super::{
    Result,
    database::{Database, field, identifier},
    ensure,
    knowledge::{Knowledge, keys},
    policy, sha,
};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};

pub fn initialise(db: &Database) -> Result<()> {
    db.conn.execute_batch("CREATE TABLE IF NOT EXISTS native_chat(scope TEXT NOT NULL,chat_id TEXT NOT NULL,state TEXT NOT NULL,version INTEGER NOT NULL,PRIMARY KEY(scope,chat_id));
        CREATE TABLE IF NOT EXISTS native_chat_link(scope TEXT NOT NULL,memory_id BLOB NOT NULL REFERENCES cortex_memory(memory_id) ON DELETE CASCADE,path TEXT NOT NULL,chat_id TEXT NOT NULL,PRIMARY KEY(scope,memory_id,path),FOREIGN KEY(scope,chat_id) REFERENCES native_chat(scope,chat_id));
        CREATE INDEX IF NOT EXISTS native_chat_link_chat ON native_chat_link(scope,chat_id,memory_id);
        CREATE INDEX IF NOT EXISTS native_chat_link_memory ON native_chat_link(memory_id);")?;
    let registered: bool = db.conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='native_chat_source')",
        [],
        |r| r.get(0),
    )?;
    db.conn.execute_batch("CREATE TABLE IF NOT EXISTS native_chat_source(scope TEXT NOT NULL,path_hash BLOB NOT NULL,chat_id TEXT NOT NULL,PRIMARY KEY(scope,path_hash),FOREIGN KEY(scope,chat_id) REFERENCES native_chat(scope,chat_id));")?;
    db.conn.execute_batch("CREATE TABLE IF NOT EXISTS native_chat_path(scope TEXT NOT NULL,path TEXT NOT NULL,chat_id TEXT NOT NULL,PRIMARY KEY(scope,path));
        CREATE INDEX IF NOT EXISTS native_chat_path_owner ON native_chat_path(scope,chat_id);
        INSERT OR IGNORE INTO native_chat_path SELECT DISTINCT scope,path,chat_id FROM native_chat_link;")?;
    if !registered {
        let rows = db
            .conn
            .prepare("SELECT DISTINCT scope,path,chat_id FROM native_chat_link")?
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                ))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        for (scope, path, chat) in rows {
            register_source(db, &scope, &path, &chat)?;
        }
    }
    Ok(())
}

fn register_source(db: &Database, scope: &str, path: &str, chat: &str) -> Result<()> {
    let digest = sha(path.as_bytes());
    let previous: Option<String> = db
        .conn
        .query_row(
            "SELECT chat_id FROM native_chat_source WHERE scope=? AND path_hash=?",
            params![scope, digest],
            |r| r.get(0),
        )
        .optional()?;
    ensure(
        previous.as_ref().is_none_or(|old| old == chat),
        "source provenance cannot be reassigned",
    )?;
    db.conn.execute(
        "INSERT OR IGNORE INTO native_chat_source VALUES(?,?,?)",
        params![scope, digest, chat],
    )?;
    db.conn.execute(
        "INSERT OR IGNORE INTO native_chat_path VALUES(?,?,?)",
        params![scope, path, chat],
    )?;
    Ok(())
}

pub fn bind_source(k: &Knowledge, id: &str, path: &str) -> Result<()> {
    let chat:Option<(String,String)>=k.db.conn.query_row("SELECT c.chat_id,c.state FROM native_chat_source s JOIN native_chat c USING(scope,chat_id) WHERE s.scope=? AND s.path_hash=?",params![k.session.scope,sha(path.as_bytes())],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if let Some((chat, state)) = chat {
        ensure(state == "active", "source chat is not active")?;
        k.db.conn.execute(
            "INSERT OR IGNORE INTO native_chat_link VALUES(?,?,?,?)",
            params![k.session.scope, identifier(id)?, path, chat],
        )?;
    }
    Ok(())
}

pub fn source_allowed(k: &Knowledge, id: &str, path: &str) -> Result<bool> {
    let exists: bool = k.db.conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='native_chat_link')",
        [],
        |r| r.get(0),
    )?;
    if !exists {
        return Ok(true);
    }
    let state:Option<String>=k.db.conn.query_row("SELECT c.state FROM native_chat_link l JOIN native_chat c USING(scope,chat_id) WHERE l.scope=? AND l.memory_id=? AND l.path=?",params![k.session.scope,identifier(id)?,path],|r|r.get(0)).optional()?;
    Ok(state.is_none_or(|s| s == "active"))
}

pub fn execute(k: &Knowledge, action: &str, a: &Value) -> Result<Value> {
    match action {
        "chat-link" => {
            keys(a, &["chat_id", "memory_id", "path"], &[])?;
            ensure(k.session.generate_memories, "memory generation disabled")?;
            let id = field(a, "memory_id")?;
            let chat = field(a, "chat_id")?;
            let path = field(a, "path")?;
            policy::check_text(chat, 128)?;
            ensure(!chat.trim().is_empty(), "chat identity required")?;
            k.db.memory(&k.session.scope, id, true)?;
            let state: String = k.db.conn.query_row(
                "SELECT state FROM native_chat WHERE scope=? AND chat_id=?",
                params![k.session.scope, chat],
                |r| r.get(0),
            )?;
            ensure(state == "active", "source chat is not active")?;
            ensure(
                k.source_items(&[id.into()])?
                    .iter()
                    .any(|s| s["payload"]["path"] == path),
                "existing source binding required",
            )?;
            let previous: Option<String> = k
                .db
                .conn
                .query_row(
                    "SELECT chat_id FROM native_chat_link WHERE scope=? AND memory_id=? AND path=?",
                    params![k.session.scope, identifier(id)?, path],
                    |r| r.get(0),
                )
                .optional()?;
            ensure(
                previous.as_ref().is_none_or(|c| c == chat),
                "source provenance cannot be reassigned",
            )?;
            register_source(k.db, &k.session.scope, path, chat)?;
            k.db.conn.execute(
                "INSERT OR IGNORE INTO native_chat_link VALUES(?,?,?,?)",
                params![k.session.scope, identifier(id)?, path, chat],
            )?;
            // Link earlier copies of this same evidence path as well as future bindings.
            let copies=k.db.conn.prepare("SELECT owner FROM knowledge_item WHERE scope=? AND kind='source' AND json_extract(payload,'$.path')=?")?
                .query_map(params![k.session.scope,path],|r|r.get::<_,Vec<u8>>(0))?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            for copy in copies {
                bind_source(k, &hex::encode(copy), path)?;
            }
            Ok(json!({"linked":true}))
        }
        "chat-event" | "chat-drain" => {
            keys(a, &["chat_id", "state", "version"], &[])?;
            let chat = field(a, "chat_id")?;
            let state = field(a, "state")?;
            let version = a["version"].as_i64().ok_or("lifecycle version required")?;
            policy::check_text(chat, 128)?;
            ensure(
                !chat.trim().is_empty() && version > 0,
                "invalid lifecycle identity or version",
            )?;
            ensure(
                ["active", "archived", "deleted", "unknown"].contains(&state),
                "unsupported chat state",
            )?;
            let previous: Option<(String, i64)> =
                k.db.conn
                    .query_row(
                        "SELECT state,version FROM native_chat WHERE scope=? AND chat_id=?",
                        params![k.session.scope, chat],
                        |r| Ok((r.get(0)?, r.get(1)?)),
                    )
                    .optional()?;
            if let Some((old, seen)) = &previous {
                if version < *seen {
                    return Ok(json!({"applied":false,"stale":true}));
                }
                if action == "chat-drain" {
                    ensure(
                        old == "deleted" && state == "deleted" && version == *seen,
                        "invalid cleanup state",
                    )?;
                } else if version == *seen {
                    ensure(old == state, "lifecycle version collision")?;
                    return Ok(json!({"applied":false,"duplicate":true}));
                }
                ensure(
                    old != "deleted" || state == "deleted",
                    "deleted chat cannot be resurrected",
                )?;
            }
            k.db.conn.execute("INSERT INTO native_chat VALUES(?,?,?,?) ON CONFLICT(scope,chat_id) DO UPDATE SET state=excluded.state,version=excluded.version",params![k.session.scope,chat,state,version])?;
            super::retention::chat_transition(k, chat, state)?;
            let affected = k
                .db
                .conn
                .prepare(
                    "SELECT DISTINCT memory_id FROM native_chat_link WHERE scope=? AND chat_id=? LIMIT ?",
                )?
                .query_map(params![k.session.scope, chat, if action == "chat-drain" {32} else {i64::MAX}], |r| r.get::<_, Vec<u8>>(0))?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            let configured: bool = k.db.conn.query_row(
                "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='native_vector_config')",
                [],
                |r| r.get(0),
            )?;
            let mut purged = 0;
            for raw_id in &affected {
                let id = hex::encode(raw_id);
                if state == "deleted" {
                    let sources = k.source_items(std::slice::from_ref(&id))?;
                    // The sweeper drains linked exact copies in their own bounded batches.
                    if action == "chat-drain" {
                        let waiting:bool=k.db.conn.query_row("SELECT EXISTS(SELECT 1 FROM cortex_verbatim v WHERE v.scope=? AND v.linked_memory_id=?) AND NOT EXISTS(SELECT 1 FROM knowledge_item i WHERE i.scope=? AND i.owner=? AND i.kind='source' AND NOT EXISTS(SELECT 1 FROM native_chat_path p WHERE p.scope=i.scope AND p.chat_id=? AND p.path=json_extract(i.payload,'$.path')))",params![k.session.scope,raw_id,k.session.scope,raw_id,chat],|r|r.get(0))?;
                        if waiting {
                            continue;
                        }
                    }
                    for source in sources {
                        let belongs:bool=k.db.conn.query_row("SELECT EXISTS(SELECT 1 FROM native_chat_link WHERE scope=? AND memory_id=? AND path=? AND chat_id=?)",params![k.session.scope,raw_id,field(&source["payload"],"path")?,chat],|r|r.get(0))?;
                        if belongs {
                            k.db.conn.execute(
                                "DELETE FROM knowledge_item WHERE scope=? AND id=?",
                                params![k.session.scope, field(&source, "id")?],
                            )?;
                        }
                    }
                    k.db.conn.execute(
                        "DELETE FROM native_chat_link WHERE scope=? AND memory_id=? AND chat_id=?",
                        params![k.session.scope, raw_id, chat],
                    )?;
                    let surviving = k.source_items(std::slice::from_ref(&id))?;
                    if surviving.is_empty() {
                        super::retention::purge_linked_exact(k, raw_id)?;
                        super::admin::execute(
                            k,
                            &json!({"action":"purge","arguments":{"memory_id":id,"user_confirmed":true}}),
                        )?;
                        purged += 1;
                        continue;
                    }
                    let primary = &surviving[0]["payload"];
                    k.db.replace_source(
                        &k.session.scope,
                        &id,
                        field(primary, "path")?,
                        field(primary, "sha256")?,
                    )?;
                }
                if configured {
                    // Lifecycle changes invalidate derivatives before their acknowledgement.
                    k.db.conn.execute(
                        "DELETE FROM native_vector WHERE scope=? AND memory_id=?",
                        params![k.session.scope, raw_id],
                    )?;
                    k.db.conn.execute(
                        "DELETE FROM native_vector_job WHERE scope=? AND memory_id=?",
                        params![k.session.scope, raw_id],
                    )?;
                    if k.freshness(&id).is_ok_and(|s| s["state"] == "fresh") {
                        k.db.conn.execute("INSERT OR IGNORE INTO native_vector_job SELECT memory_id,scope FROM cortex_memory WHERE scope=? AND memory_id=? AND status=0 AND EXISTS(SELECT 1 FROM native_vector_config WHERE scope=?)",params![k.session.scope,raw_id,k.session.scope])?;
                    }
                }
            }
            // Revokes cached write receipts that could otherwise re-deliver removed content.
            if state == "deleted" {
                super::recovery::revoke_scope(k.db, &k.session.scope)?;
            }
            Ok(
                json!({"applied":true,"state":state,"version":version,"affected":affected.len(),"purged":purged,
                "capture":"explicit_host_receipt","backup_erasure":false}),
            )
        }
        _ => Err("unsupported lifecycle operation".into()),
    }
}
