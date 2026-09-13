use super::{
    Result, after_days, codec,
    database::{Database, Row, boolean, field, identifier, optional_time, quality, sql_json, text},
    ensure, exact,
    knowledge::{Knowledge, checked, keys},
    now, policy, sha,
};
use base64::Engine;
use rusqlite::{params, types::Value as Sql};
use serde_json::{Value, json as j};
use std::collections::BTreeSet;

const TABLES: [(&str, &str); 3] = [
    ("cortex_memory", "memory_id"),
    ("cortex_verbatim", "archive_id"),
    ("hippocampus_stage", "stage_id"),
];
pub enum Prepared {
    None,
    Memory(super::database::PreparedMemory),
    Stage(Vec<u8>),
    Batch(Vec<Prepared>),
}
pub fn prepare(a: &Value) -> Result<Prepared> {
    Ok(match a["action"].as_str() {
        Some("remember" | "remember-bound") => {
            Prepared::Memory(Database::prepare_memory(&a["arguments"])?)
        }
        Some("stage") => {
            let raw = field(&a["arguments"], "text")?;
            policy::check_text(raw, policy::MAX_TEXT)?;
            Prepared::Stage(codec::compress(raw.as_bytes())?)
        }
        Some("batch") => {
            keys(&a["arguments"], &["items"], &[])?;
            let items = a["arguments"]["items"]
                .as_array()
                .ok_or("batch items required")?;
            ensure(
                (1..=8).contains(&items.len()),
                "batch must contain one to eight writes",
            )?;
            let mut prepared = Vec::new();
            for item in items {
                ensure(
                    matches!(
                        item["action"].as_str(),
                        Some("remember" | "remember-bound" | "stage" | "lifecycle")
                    ),
                    "unsupported batch action",
                )?;
                prepared.push(prepare(item)?);
            }
            Prepared::Batch(prepared)
        }
        _ => Prepared::None,
    })
}
fn number(a: &Value, k: &str, default: i64, min: i64, max: i64) -> Result<i64> {
    let n = match a.get(k) {
        None => default,
        Some(v) => v.as_i64().ok_or("invalid integer")?,
    };
    ensure((min..=max).contains(&n), "integer out of range")?;
    Ok(n)
}
fn status(n: i64) -> Result<&'static str> {
    ["active", "superseded", "deleted", "expired", "archived"]
        .get(n as usize)
        .copied()
        .ok_or_else(|| "invalid status".into())
}
fn kind(n: i64) -> Result<&'static str> {
    [
        "semantic",
        "episodic",
        "procedural",
        "priming_conditioning",
        "classical_conditioning",
    ]
    .get(n.wrapping_sub(1) as usize)
    .copied()
    .ok_or_else(|| "invalid memory type".into())
}
fn find(db: &Database, scope: &str, id: &str) -> Result<(&'static str, &'static str, Row)> {
    let id = identifier(id)?;
    let mut found = Vec::new();
    for (table, key) in TABLES {
        for row in db.rows(
            &format!("SELECT * FROM {table} WHERE {key}=? AND scope=?"),
            vec![Sql::Blob(id.clone()), Sql::Text(scope.into())],
        )? {
            row.verify()?;
            found.push((table, key, row));
        }
    }
    ensure(found.len() == 1, "scoped identity missing or ambiguous")?;
    Ok(found.remove(0))
}
pub fn lifecycle(k: &Knowledge, a: &Value) -> Result<Value> {
    keys(a, &["memory_id", "action"], &["renew", "expires"])?;
    let id = field(a, "memory_id")?;
    let action = field(a, "action")?;
    let (table, key, row) = find(k.db, &k.session.scope, id)?;
    ensure(
        table != "hippocampus_stage",
        "stage has no lifecycle restoration",
    )?;
    let old = row.int("status")?;
    let (allowed, new) = match action {
        "restore" => (vec![2, 3], 0),
        "unarchive" => (vec![4], 0),
        "forget" => (vec![0, 1, 3, 4], 2),
        "archive" => (vec![0], 4),
        "pin" | "unpin" => (vec![0, 4], old),
        "retention" => (vec![0, 2, 3, 4], old),
        _ => return Err("invalid lifecycle action".into()),
    };
    ensure(allowed.contains(&old), "lifecycle transition not allowed")?;
    let mut expires = row.optional("expires_at")?.map(String::from);
    let clock = now();
    if ["restore", "unarchive"].contains(&action) {
        if table == "cortex_memory" {
            k.db.verify_memory(&row)?;
            k.db.detail(&row)?;
            ensure(k.db.conn.query_row("SELECT count(*) FROM cortex_memory WHERE scope=? AND claim_id=? AND status=0 AND memory_id!=?",params![k.session.scope,row.text("claim_id")?,row.bytes(key)?],|r|r.get::<_,i64>(0))?==0,"active claim conflict")?;
            ensure(
                row.optional("valid_to")?.is_none_or(|v| v > clock.as_str()),
                "historical validity ended",
            )?;
        } else {
            exact::verify(&row)?;
        }
        if expires.as_ref().is_some_and(|s| s <= &clock) {
            expires = optional_time(a, "renew")?;
            ensure(
                expires.as_ref().is_some_and(|s| s > &clock),
                "future renewal required",
            )?;
        }
    }
    if action == "retention" {
        expires = optional_time(a, "expires")?;
        ensure(
            expires.as_ref().is_none_or(|s| s > &clock),
            "future retention required",
        )?;
    }
    k.db.conn.execute(
        &format!("UPDATE {table} SET status=?,updated_at=?,expires_at=? WHERE {key}=? AND scope=?"),
        params![new, clock, expires, row.bytes(key)?, k.session.scope],
    )?;
    if ["pin", "unpin"].contains(&action) {
        k.db.conn.execute(
            &format!("UPDATE {table} SET pinned=? WHERE {key}=?"),
            params![i64::from(action == "pin"), row.bytes(key)?],
        )?;
    }
    if table == "cortex_verbatim" {
        k.db.conn.execute(
            "UPDATE cortex_verbatim SET retention=? WHERE archive_id=?",
            params![
                if expires.is_some() {
                    "expiring"
                } else {
                    "until_user_deletes"
                },
                row.bytes(key)?
            ],
        )?;
    }
    k.db.seal(table, key, row.bytes(key)?)?;
    Ok(j!({"memory_id":id,"status":status(new)?}))
}
fn dispose(db: &Database, id: &[u8], status: i64) -> Result<()> {
    db.conn.execute("UPDATE hippocampus_stage SET status=?,raw_blob=?,raw_bytes=0,stored_bytes=0,checksum_sha256=? WHERE stage_id=?",params![status,Vec::<u8>::new(),sha(&[]),id])?;
    db.seal("hippocampus_stage", "stage_id", id)
}
fn stage(k: &Knowledge, a: &Value, prepared: &Prepared) -> Result<Value> {
    keys(a, &["text"], &["source", "expires"])?;
    let raw = field(a, "text")?;
    policy::check_text(raw, policy::MAX_TEXT)?;
    ensure(!raw.trim().is_empty(), "empty stage")?;
    let source = text(a, "source", "user")?;
    super::retention::check_source(k, source)?;
    policy::check_text(source, policy::MAX_TEXT)?;
    let expires = optional_time(a, "expires")?.unwrap_or(after_days(1, &now())?);
    let blob = if let Prepared::Stage(blob) = prepared {
        blob.clone()
    } else {
        codec::compress(raw.as_bytes())?
    };
    let id = uuid::Uuid::new_v4().as_bytes().to_vec();
    k.db.conn.execute("INSERT INTO hippocampus_stage(stage_id,created_at,expires_at,scope,source,raw_blob,raw_bytes,stored_bytes,checksum_sha256) VALUES(?,?,?,?,?,?,?,?,?)",params![id,now(),expires,k.session.scope,source,blob,raw.len() as i64,blob.len() as i64,sha(raw.as_bytes())])?;
    k.db.seal("hippocampus_stage", "stage_id", &id)?;
    Ok(
        j!({"stage_id":hex::encode(id),"raw_bytes":raw.len(),"stored_bytes":blob.len(),"scope":k.session.scope,"expires_at":expires}),
    )
}
fn purge(k: &Knowledge, a: &Value) -> Result<Value> {
    keys(a, &["memory_id", "user_confirmed"], &[])?;
    ensure(
        boolean(a, "user_confirmed", false)?,
        "purge confirmation required",
    )?;
    let id = field(a, "memory_id")?;
    let (table, key, row) = find(k.db, &k.session.scope, id)?;
    let bytes = row.bytes(key)?;
    if table == "cortex_memory" {
        for (table, key, link) in [
            ("cortex_memory", "memory_id", "supersedes_id"),
            ("cortex_verbatim", "archive_id", "linked_memory_id"),
        ] {
            for child in k.db.rows(
                &format!("SELECT * FROM {table} WHERE {link}=?"),
                vec![Sql::Blob(bytes.to_vec())],
            )? {
                child.verify()?;
                ensure(
                    child.text("scope")? == k.session.scope,
                    "cross-scope purge dependency",
                )?;
                k.db.conn.execute(
                    &format!("UPDATE {table} SET {link}=NULL WHERE {key}=?"),
                    [child.bytes(key)?],
                )?;
                k.db.seal(table, key, child.bytes(key)?)?;
            }
        }
    }
    k.db.conn.execute(
        "INSERT OR REPLACE INTO cortex_tombstone VALUES(?,?,?)",
        params![bytes, k.session.scope, now()],
    )?;
    ensure(
        k.db.conn.execute(
            &format!("DELETE FROM {table} WHERE {key}=? AND scope=?"),
            params![bytes, k.session.scope],
        )? == 1,
        "purge must remove one record",
    )?;
    super::recovery::revoke_scope(k.db, &k.session.scope)?;
    Ok(
        j!({"memory_id":id,"status":"purged","records_removed":1,"checkpoint":null,"prototype_notice":"Record removal does not guarantee erasure from backups, retained copies or filesystem history."}),
    )
}
fn expire(k: &Knowledge, a: &Value) -> Result<Value> {
    keys(a, &[], &[])?;
    let mut counts = serde_json::Map::new();
    for (table, key, label) in [
        ("hippocampus_stage", "stage_id", "hippocampus_expired"),
        ("cortex_memory", "memory_id", "cortex_expired"),
        ("cortex_verbatim", "archive_id", "verbatim_expired"),
    ] {
        let rows=k.db.rows(&format!("SELECT * FROM {table} WHERE scope=? AND status=0 AND expires_at IS NOT NULL AND expires_at<=?"),vec![Sql::Text(k.session.scope.clone()),Sql::Text(now())])?;
        for row in &rows {
            row.verify()?;
            if table == "hippocampus_stage" {
                dispose(k.db, row.bytes(key)?, 2)?;
            } else {
                k.db.conn.execute(
                    &format!("UPDATE {table} SET status=3,updated_at=? WHERE {key}=?"),
                    params![now(), row.bytes(key)?],
                )?;
                k.db.seal(table, key, row.bytes(key)?)?;
            }
        }
        counts.insert(label.into(), j!(rows.len()));
    }
    k.expire()?;
    Ok(Value::Object(counts))
}
fn list(k: &Knowledge, a: &Value) -> Result<Value> {
    keys(a, &[], &["kind", "status", "limit", "offset"])?;
    let kind_name = text(a, "kind", "semantic")?;
    let (table, key, sort) = match kind_name {
        "semantic" => ("cortex_memory", "memory_id", "updated_at"),
        "exact" => ("cortex_verbatim", "archive_id", "updated_at"),
        "stage" => ("hippocampus_stage", "stage_id", "created_at"),
        _ => return Err("unsupported kind".into()),
    };
    let limit = number(a, "limit", 20, 1, 100)?;
    let offset = number(a, "offset", 0, 0, 1_000_000)?;
    let mut sql = format!("SELECT * FROM {table} WHERE scope=?");
    let mut values = vec![Sql::Text(k.session.scope.clone())];
    let names = if kind_name == "stage" {
        vec!["active", "consolidated", "expired"]
    } else {
        vec!["active", "superseded", "deleted", "expired", "archived"]
    };
    if let Some(s) = a.get("status").filter(|v| !v.is_null()) {
        let n = names
            .iter()
            .position(|n| j!(n) == *s)
            .ok_or("invalid status filter")?;
        sql.push_str(" AND status=?");
        values.push(Sql::Integer(n as i64));
    }
    sql.push_str(&format!(" ORDER BY {sort} DESC,{key} LIMIT ? OFFSET ?"));
    values.extend([Sql::Integer(limit + 1), Sql::Integer(offset)]);
    let rows = k.db.rows(&sql, values)?;
    let mut items = Vec::new();
    for row in rows.iter().take(limit as usize) {
        row.verify()?;
        let id = hex::encode(row.bytes(key)?);
        let state = names
            .get(row.int("status")? as usize)
            .ok_or("invalid status")?;
        if kind_name == "semantic" {
            let p = k.db.verify_memory(row)?;
            items.push(j!({"memory_id":id,"type":kind(row.int("memory_type")?)?,"scope":k.session.scope,"subject":p[0],"summary":p[1],"status":state,"pinned":row.int("pinned")?!=0,"updated_at":row.text("updated_at")?,"observed_at":row.text("observed_at")?,"prior_version_id":match row.0.get("prior_version_id"){Some(Sql::Blob(b))=>j!(hex::encode(b)),_=>Value::Null}}));
        } else {
            if kind_name == "exact" {
                exact::verify(row)?;
            } else {
                let raw = codec::decompress(row.bytes("raw_blob")?)?;
                ensure(
                    sha(&raw) == row.bytes("checksum_sha256")?,
                    "stage integrity failure",
                )?;
                policy::check_text(std::str::from_utf8(&raw)?, policy::MAX_TEXT)?;
            }
            items.push(j!({"memory_id":id,"kind":kind_name,"scope":k.session.scope,"source":row.text("source")?,"expires_at":row.value("expires_at")?,"status":state,"bytes":row.int(if kind_name=="exact"{"original_bytes"}else{"raw_bytes"})?}));
        }
    }
    Ok(
        j!({"scope":k.session.scope,"kind":kind_name,"count":items.len(),"memories":items,"has_more":rows.len()>limit as usize}),
    )
}
fn stats(k: &Knowledge) -> Result<Value> {
    let mut result = j!({"scope":k.session.scope,"token_savings_measured":false});
    let (mut raw, mut stored) = (0, 0);
    for (table, r, s, label) in [
        (
            "hippocampus_stage",
            "raw_bytes",
            "stored_bytes",
            "hippocampus_records",
        ),
        (
            "cortex_memory",
            "payload_raw_bytes",
            "payload_stored_bytes",
            "cortex_records",
        ),
        (
            "cortex_verbatim",
            "original_bytes",
            "stored_bytes",
            "full_fidelity_records",
        ),
    ] {
        let(n,r,s):(i64,i64,i64)=k.db.conn.query_row(&format!("SELECT count(*),coalesce(sum({r}),0),coalesce(sum({s}),0) FROM {table} WHERE scope=?"),[&k.session.scope],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
        raw += r;
        stored += s;
        result[label] = j!(n);
    }
    for row in k.db.rows(
        "SELECT d.* FROM cortex_detail d JOIN cortex_memory m USING(memory_id) WHERE m.scope=?",
        vec![Sql::Text(k.session.scope.clone())],
    )? {
        raw += codec::decompress(row.bytes("detail_blob")?)?.len() as i64;
        stored += row.bytes("detail_blob")?.len() as i64;
    }
    let pragma = |name: &str| -> Result<i64> {
        let value: Sql =
            k.db.conn
                .query_row(&format!("PRAGMA {name}"), [], |r| r.get(0))?;
        // SQLCipher exposes its page size as numeric text, unlike ordinary SQLite.
        match value {
            Sql::Integer(value) => Ok(value),
            Sql::Text(value) => Ok(value.parse::<i64>()?),
            _ => Err("invalid storage statistics pragma".into()),
        }
    };
    result["logical_payload_bytes"] = j!(raw);
    result["stored_payload_bytes"] = j!(stored);
    result["binary_compression_ratio"] = if stored == 0 {
        Value::Null
    } else {
        j!(((raw as f64 / stored as f64) * 1000.0).round_ties_even() / 1000.0)
    };
    result["whole_database_allocated_bytes"] = j!(pragma("page_count")? * pragma("page_size")?);
    result["whole_database_free_pages"] = j!(pragma("freelist_count")?);
    result["scope_term_rows"] = j!(k.db.conn.query_row(
        "SELECT count(*) FROM cortex_term t JOIN cortex_memory m USING(memory_pk) WHERE m.scope=?",
        [&k.session.scope],
        |r| r.get::<_, i64>(0)
    )?);
    Ok(result)
}
fn prune(k: &Knowledge, a: &Value) -> Result<Value> {
    keys(
        a,
        &[],
        &[
            "importance_below",
            "older_than_days",
            "limit",
            "apply",
            "user_confirmed",
            "reviewed_ids",
        ],
    )?;
    let apply = boolean(a, "apply", false)?;
    let threshold = quality(a, "importance_below", 0.25)?;
    let days = number(a, "older_than_days", 90, 0, 365000)?;
    let limit = number(a, "limit", 20, 1, 100)?;
    let cutoff = after_days(-days, &now())?;
    let reviewed = a.get("reviewed_ids").filter(|v| !v.is_null());
    ensure(
        !apply || (boolean(a, "user_confirmed", false)? && reviewed.is_some()),
        "reviewed prune confirmation required",
    )?;
    let rows = if let Some(ids) = reviewed {
        let ids = ids.as_array().ok_or("invalid review ids")?;
        ensure(
            !ids.is_empty() && ids.len() <= limit as usize,
            "invalid review count",
        )?;
        let mut seen = BTreeSet::new();
        let mut rows = Vec::new();
        for v in ids {
            let token = v.as_str().ok_or("invalid review token")?;
            ensure(seen.insert(token), "duplicate review token")?;
            let (id, sum) = token.split_once(':').ok_or("invalid review token")?;
            let (row, _) = k.db.memory(&k.session.scope, id, false)?;
            ensure(
                hex::encode(row.bytes("record_checksum")?) == sum,
                "stale review token",
            )?;
            rows.push(row);
        }
        rows
    } else {
        k.db.rows("SELECT * FROM cortex_memory WHERE scope=? AND status=0 AND pinned=0 AND memory_type!=3 AND created_at<=? AND importance<=? ORDER BY importance,created_at,memory_id LIMIT ?",vec![Sql::Text(k.session.scope.clone()),Sql::Text(cutoff.clone()),Sql::Integer(threshold),Sql::Integer(limit)])?
    };
    let mut candidates = Vec::new();
    for row in rows {
        let p = k.db.verify_memory(&row)?;
        ensure(
            row.int("status")? == 0
                && row.int("pinned")? == 0
                && row.int("memory_type")? != 3
                && row.text("created_at")? <= cutoff.as_str()
                && row.int("importance")? <= threshold,
            "prune criteria changed",
        )?;
        let id = hex::encode(row.bytes("memory_id")?);
        candidates.push(j!({"memory_id":id,"review_token":format!("{id}:{}",hex::encode(row.bytes("record_checksum")?)),"scope":k.session.scope,"type":kind(row.int("memory_type")?)?,"summary":p[1],"importance":(row.int("importance")? as f64/255.0*1000.0).round_ties_even()/1000.0,"created_at":row.text("created_at")?,"proposed_action":"archive"}));
        if apply {
            lifecycle(k, &j!({"memory_id":id,"action":"archive"}))?;
        }
    }
    Ok(
        j!({"mode":if apply{"applied"}else{"preview"},"count":candidates.len(),"candidates":candidates}),
    )
}
pub fn is_read(a: &Value) -> bool {
    matches!(
        a["action"].as_str(),
        Some(
            "export"
                | "verify"
                | "inspect"
                | "list"
                | "stats"
                | "recall-exact"
                | "recall"
                | "knowledge-page"
                | "vector-jobs"
                | "vector-status"
                | "vector-recall"
                | "archive-retention-status"
        )
    ) || (a["action"] == "prune" && a["arguments"]["apply"] != true)
}
pub fn execute(k: &Knowledge, a: &Value) -> Result<Value> {
    execute_prepared(k, a, &Prepared::None)
}
pub fn execute_prepared(k: &Knowledge, a: &Value, prepared: &Prepared) -> Result<Value> {
    keys(a, &["action", "arguments"], &[])?;
    let op = field(a, "action")?;
    let a = &a["arguments"];
    ensure(a.is_object(), "arguments required")?;
    match op {
        "project-event" | "archive-retention" | "archive-retention-status" | "archive-cleanup" => {
            super::retention::execute(k, op, a)
        }
        op if op.starts_with("vector-") => super::vectors::execute(k, op, a),
        "chat-link" | "chat-event" => super::chat_lifecycle::execute(k, op, a),
        "batch" => {
            let owned;
            let children = if let Prepared::Batch(children) = prepared {
                children
            } else {
                owned = prepare(&j!({"action":"batch","arguments":a}))?;
                let Prepared::Batch(children) = &owned else {
                    unreachable!()
                };
                children
            };
            let mut results = Vec::new();
            for (item, child) in a["items"]
                .as_array()
                .ok_or("invalid batch")?
                .iter()
                .zip(children)
            {
                // The enclosing request is all-or-nothing; no item is acknowledged early.
                results.push(execute_prepared(k, item, child)?);
            }
            Ok(j!({"atomic":true,"results":results}))
        }
        "remember" | "remember-bound" => {
            super::retention::check_source(k, text(a, "source", "user")?)?;
            keys(
                a,
                &["type", "subject", "summary"],
                &[
                    "detail",
                    "keywords",
                    "source",
                    "confidence_reason",
                    "importance",
                    "confidence",
                    "sensitivity",
                    "user_confirmed",
                    "pinned",
                    "observed_at",
                    "valid_from",
                    "valid_to",
                    "expires",
                    "source_hash",
                    "claim_id",
                    "supersedes",
                    "stage_id",
                ],
            )?;
            let result = if let Prepared::Memory(memory) = prepared {
                k.db.remember_prepared(&k.session.scope, a, memory)
            } else {
                k.db.remember(&k.session.scope, a)
            }?;
            super::chat_lifecycle::bind_source(
                k,
                field(&result, "memory_id")?,
                text(a, "source", "user")?,
            )?;
            if op == "remember-bound" {
                k.bind(
                    field(&result, "memory_id")?,
                    field(a, "source")?,
                    field(a, "source_hash")?,
                )?;
            }
            Ok(result)
        }
        "stage" => stage(k, a, prepared),
        "lifecycle" => lifecycle(k, a),
        "purge" => purge(k, a),
        "expire" => expire(k, a),
        "list" => list(k, a),
        "knowledge-page" => {
            keys(a, &[], &["after", "revision", "limit"])?;
            let after = text(a, "after", "")?;
            let revision: i64 =
                k.db.conn
                    .query_row("SELECT revision FROM vault_state", [], |r| r.get(0))?;
            if !after.is_empty() || a.get("revision").is_some() {
                ensure(
                    a["revision"].as_i64() == Some(revision),
                    "page revision changed; restart traversal",
                )?;
            }
            let items = k.page(after, number(a, "limit", 100, 1, 200)? as usize)?;
            let next = items.last().map(|i| i["id"].clone()).unwrap_or(Value::Null);
            Ok(j!({"items":items,"next_after":next,"revision":revision,"scope":k.session.scope}))
        }
        "prune" => prune(k, a),
        "stats" => {
            keys(a, &[], &[])?;
            stats(k)
        }
        "export" => {
            keys(a, &[], &[])?;
            k.export(false)
        }
        "import" => {
            keys(a, &["package", "reviewed"], &[])?;
            k.import(&a["package"], boolean(a, "reviewed", false)?)
        }
        "verify" => {
            keys(a, &[], &[])?;
            k.db.verify_scope(&k.session.scope)?;
            k.verify_items()?;
            Ok(j!({"verified":true,"scope":k.session.scope}))
        }
        "store-exact" => {
            super::retention::check_source(k, text(a, "source", "user")?)?;
            keys(
                a,
                &["text", "user_confirmed"],
                &[
                    "source",
                    "media_type",
                    "retention",
                    "expires",
                    "pinned",
                    "linked_memory_id",
                ],
            )?;
            k.db.store_exact(&k.session.scope, a)
        }
        "recall-exact" => {
            keys(a, &["archive_id"], &["offset", "length"])?;
            let (_, raw) =
                k.db.exact_bytes(&k.session.scope, field(a, "archive_id")?)?;
            let start = number(a, "offset", 0, 0, raw.len() as i64)? as usize;
            let length =
                number(a, "length", raw.len() as i64, 0, policy::MAX_EXACT as i64)? as usize;
            let end = raw.len().min(start + length);
            Ok(
                j!({"base64":base64::engine::general_purpose::STANDARD.encode(&raw[start..end]),"bytes":end-start}),
            )
        }
        "inspect" => {
            keys(a, &["memory_id"], &[])?;
            k.db.verify_scope(&k.session.scope)?;
            let (table, _, row) = find(k.db, &k.session.scope, field(a, "memory_id")?)?;
            let mut data = serde_json::Map::new();
            for (key, v) in &row.0 {
                if !["memory_pk", "payload_blob", "original_blob", "raw_blob"]
                    .contains(&key.as_str())
                {
                    data.insert(key.clone(), sql_json(v, true)?);
                }
            }
            if table == "cortex_memory" {
                let fields = k.db.verify_memory(&row)?;
                for (key, v) in ["subject", "summary", "detail", "keywords", "source"]
                    .into_iter()
                    .zip(fields)
                {
                    data.insert(key.into(), j!(v));
                }
                data.insert("detail".into(), j!(k.db.detail(&row)?));
            }
            Ok(j!({"kind":table,"record":data}))
        }
        "aliases" => {
            keys(a, &["groups", "reviewed"], &[])?;
            ensure(boolean(a, "reviewed", false)?, "reviewed aliases required")?;
            super::knowledge::alias_groups(&a["groups"])?;
            k.items(Some("aliases"))?;
            k.db.conn.execute(
                "DELETE FROM knowledge_item WHERE scope=? AND kind='aliases'",
                [&k.session.scope],
            )?;
            k.put("aliases", j!({"groups":a["groups"]}), None, None)
        }
        "relate" => {
            keys(
                a,
                &["owner", "target", "relation", "evidence", "reviewed"],
                &[],
            )?;
            ensure(boolean(a, "reviewed", false)?, "reviewed relation required")?;
            checked(a)?;
            let owner = field(a, "owner")?;
            let target = field(a, "target")?;
            k.db.memory(&k.session.scope, owner, true)?;
            k.db.memory(&k.session.scope, target, true)?;
            let payload = j!({"relation":a["relation"],"evidence":a["evidence"]});
            for i in k.related_items(owner)? {
                if i["owner"] == owner && i["target"] == target && i["payload"] == payload {
                    return Ok(i);
                }
            }
            k.put("relation", payload, Some(owner), Some(target))
        }
        "bind-source" => {
            keys(a, &["memory_id", "path", "sha256"], &[])?;
            k.bind(
                field(a, "memory_id")?,
                field(a, "path")?,
                field(a, "sha256")?,
            )
        }
        "propose-spans" => {
            keys(a, &["path", "spans", "sha256"], &[])?;
            k.propose_spans(field(a, "path")?, &a["spans"], field(a, "sha256")?)
        }
        "recall" => {
            keys(a, &["query"], &["mode", "limit", "max_tokens"])?;
            k.recall(
                field(a, "query")?,
                text(a, "mode", "lexical")?,
                number(a, "limit", 8, 1, 100)? as usize,
                number(a, "max_tokens", 700, 64, 8000)? as usize,
            )
        }
        _ => Err("unsupported maintenance action".into()),
    }
}
