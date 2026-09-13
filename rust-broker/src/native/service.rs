use super::{
    Result, canonical,
    database::{Database, field, identifier, text},
    ensure, json,
    knowledge::{Knowledge, checked, digest, keys, tokens},
    now,
};
use crate::{Config, Request, Session};
use rusqlite::params;
use serde_json::{Value, json as j};
use std::{
    io::{BufRead, Write},
    path::Path,
};

pub fn worker() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    ensure(
        args.len() == 4 && args[2] == "--role" && ["read", "write"].contains(&args[3].as_str()),
        "invalid native worker arguments",
    )?;
    let read = args[3] == "read";
    let mut input = std::io::stdin().lock();
    let mut output = std::io::stdout().lock();
    let raw = read_frame(&mut input)?.ok_or("missing config")?;
    let config: Config = serde_json::from_value(json(std::str::from_utf8(&raw)?)?)?;
    config.validate()?;
    let db = Database::open_keyed(Path::new(&config.database), read, config.key_env.as_deref())?;
    if !read {
        Knowledge::initialize(&db)?;
    }
    for session in &config.sessions {
        Knowledge::new(&db, session)?;
    }
    // These immutable resources are needed by normal calls. Charge their one-time
    // construction to worker startup, not the first user's recall deadline.
    tokens("");
    catalogue_schema();
    let _index_warmer = read.then(|| super::vectors::start_warmer(&config));
    let checkpoints = if read {
        None
    } else {
        let service = super::checkpoint::Checkpoints::start(&config)?;
        super::checkpoint::configure_writer(&db.conn)?;
        Some(service)
    };
    let mut last_checkpoint = std::time::Instant::now();
    writeln!(output, "{{\"ready\":true}}")?;
    output.flush()?;
    while let Some(raw) = read_frame(&mut input)? {
        let request = serde_json::from_slice::<Request>(&raw);
        let mut response = match request {
            Ok(request) => {
                let result = (|| -> Result<Value> {
                    ensure(crate::validate_request(&request), "invalid request")?;
                    ensure(
                        !read || crate::is_read(&request),
                        "read worker cannot mutate",
                    )?;
                    let session = config
                        .sessions
                        .iter()
                        .find(|s| s.id == request.session)
                        .ok_or("unknown session")?;
                    respond(&db, session, &request, read)
                })();
                match result {
                    Ok(r) => r,
                    Err(_) => {
                        j!({"session":request.session,"id":request.id,"error":"request_rejected"})
                    }
                }
            }
            Err(_) => j!({"error":"invalid_request"}),
        };
        if let Some(checkpoints) = &checkpoints {
            if last_checkpoint.elapsed().as_secs() >= 1 {
                checkpoints.request();
                last_checkpoint = std::time::Instant::now();
            }
            if let Some(receipt) = checkpoints.take() {
                response["checkpoint"] = receipt;
            }
        }
        writeln!(output, "{}", canonical(&response, false)?)?;
        output.flush()?;
    }
    Ok(())
}

pub fn read_frame<R: BufRead>(input: &mut R) -> Result<Option<Vec<u8>>> {
    let mut raw = Vec::new();
    loop {
        let buf = input.fill_buf()?;
        if buf.is_empty() {
            ensure(raw.is_empty(), "partial frame")?;
            return Ok(None);
        }
        let end = buf.iter().position(|b| *b == b'\n').map(|n| n + 1);
        let n = end.unwrap_or(buf.len());
        ensure(raw.len() + n <= crate::MAX_FRAME, "frame exceeds limit")?;
        raw.extend_from_slice(&buf[..n]);
        input.consume(n);
        if end.is_some() {
            return Ok(Some(raw));
        }
    }
}

pub fn respond(db: &Database, s: &Session, r: &Request, read: bool) -> Result<Value> {
    respond_bounded(db, s, r, read, crate::MAX_RESPONSE)
}
fn respond_bounded(
    db: &Database,
    s: &Session,
    r: &Request,
    read: bool,
    limit: usize,
) -> Result<Value> {
    let started = std::time::Instant::now();
    let prepared = if r.operation == "admin" && s.allow_admin {
        super::admin::prepare(&r.arguments)?
    } else {
        super::admin::Prepared::None
    };
    let prepared_at = std::time::Instant::now();
    db.conn
        .execute_batch(if read { "BEGIN" } else { "BEGIN IMMEDIATE" })?;
    let begun_at = std::time::Instant::now();
    let result = (|| -> Result<Value> {
        ensure(
            !read || r.recovery.is_none(),
            "read request cannot use write recovery",
        )?;
        super::retention::check_request(db, s, r)?;
        if let Some(response) = super::recovery::lookup(db, s, r)? {
            ensure(
                canonical(&response, false)?.len() + 512 < limit,
                "response exceeds bound",
            )?;
            return Ok(response);
        }
        let value = execute(db, s, r, &prepared)?;
        let response = j!({"session":r.session,"id":r.id,"result":value});
        ensure(
            canonical(&response, false)?.len() + 512 < limit,
            "response exceeds bound",
        )?;
        super::recovery::record(db, s, r, &response)?;
        Ok(response)
    })();
    match result {
        Ok(mut r) => {
            let committing_at = std::time::Instant::now();
            if let Err(e) = db.conn.execute_batch("COMMIT") {
                let _ = db.conn.execute_batch("ROLLBACK");
                return Err(e.into());
            }
            r["native_timing"] = j!({"prepare_ms":prepared_at.duration_since(started).as_secs_f64()*1000.0,
                "begin_ms":begun_at.duration_since(prepared_at).as_secs_f64()*1000.0,
                "execute_ms":committing_at.duration_since(begun_at).as_secs_f64()*1000.0,
                "commit_ms":committing_at.elapsed().as_secs_f64()*1000.0,
                "snapshot_ms":begun_at.elapsed().as_secs_f64()*1000.0});
            Ok(r)
        }
        Err(e) => {
            let _ = db.conn.execute_batch("ROLLBACK");
            Err(e)
        }
    }
}

fn execute(
    db: &Database,
    s: &Session,
    r: &Request,
    prepared: &super::admin::Prepared,
) -> Result<Value> {
    if r.operation == "routing-export"
        || (r.operation == "admin" && r.arguments["action"] == "export")
    {
        ensure(
            !db.encrypted() || s.allow_plaintext_export,
            "encrypted vault export requires explicit plaintext export policy; use encrypted backup",
        )?;
        let has_chat_table: bool = db.conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='native_chat')",
            [],
            |r| r.get(0),
        )?;
        if has_chat_table {
            let captured: bool = db.conn.query_row(
                "SELECT EXISTS(SELECT 1 FROM native_chat WHERE scope=?)",
                [&s.scope],
                |r| r.get(0),
            )?;
            ensure(
                !captured,
                "legacy JSON cannot preserve chat lifecycle; use encrypted backup",
            )?;
        }
    }
    let k = Knowledge::new(db, s)?;
    let a = &r.arguments;
    match r.operation.as_str() {
        "ping" => {
            keys(a, &[], &[])?;
            Ok(j!({"alive":true,"backend":"rust-native"}))
        }
        "catalogue" => {
            keys(a, &[], &[])?;
            Ok(j!({"tools":[catalogue_schema()["compact"]]}))
        }
        "call" => {
            let result = (|| -> Result<Value> {
                let (name, args) = normalise(a)?;
                ensure(allow_tool(s, &name), "chat policy prohibits operation")?;
                db.conn.execute_batch("SAVEPOINT native_tool")?;
                let result = (|| -> Result<Value> {
                    let data = if name == "memory_recall"
                        && a["_meta"].get("memory_embedding").is_some()
                    {
                        super::vectors::compact(
                            &k,
                            field(&args, "query")?,
                            &a["_meta"]["memory_embedding"],
                        )?
                    } else {
                        tool(&k, &name, &args)?
                    };
                    // Host-only read phase. Candidate text never becomes model-facing tool text.
                    if name == "memory_recall"
                        && a["_meta"]["memory_embedding"]["phase"] == "candidates"
                    {
                        return Ok(j!({"memory_candidates":data}));
                    }
                    let response = j!({"jsonrpc":"2.0","id":r.id,"result":{"content":[{"type":"text","text":canonical(&data,false)?}],"isError":false}});
                    ensure(
                        tokens(&canonical(&response, false)?) <= 1400,
                        "tool response exceeds token budget",
                    )?;
                    Ok(response)
                })();
                match result {
                    Ok(v) => {
                        db.conn.execute_batch("RELEASE native_tool")?;
                        Ok(v)
                    }
                    Err(e) => {
                        db.conn
                            .execute_batch("ROLLBACK TO native_tool;RELEASE native_tool")?;
                        Err(e)
                    }
                }
            })();
            Ok(match result {
                Ok(v) => v,
                Err(_) => {
                    j!({"jsonrpc":"2.0","id":r.id,"error":{"code":-32602,"message":"Request rejected by validation, scope, source freshness or response budget"}})
                }
            })
        }
        "background-propose" => {
            keys(a, &["path", "quota", "chat"], &[])?;
            background(&k, a)
        }
        "admin" => {
            ensure(s.allow_admin, "host maintenance authority required")?;
            super::admin::execute_prepared(&k, a, prepared)
        }
        op if op.starts_with("routing-") => {
            ensure(
                if crate::is_read(r) {
                    s.use_memories
                } else {
                    s.generate_memories
                },
                "chat policy prohibits operation",
            )?;
            super::routing::execute(&k, op, a)
        }
        _ => Err("unsupported operation".into()),
    }
}

pub fn command() -> Result<()> {
    use std::io::Read;
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
        config.backend == "native",
        "native command requires native backend",
    )?;
    if args[1] == "--init" {
        for s in &config.sessions {
            ensure(Path::new(&s.source_root).is_dir(), "source root must exist")?;
        }
        let file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&config.database)?;
        drop(file);
        let conn = rusqlite::Connection::open(&config.database)?;
        Database::unlock(&conn, config.key_env.as_deref())?;
        let db = Database::initialize(conn)?;
        Knowledge::initialize(&db)?;
        println!("{{\"initialised\":true,\"synthetic_only\":true,\"automatic_dispatch\":false}}");
        return Ok(());
    }
    raw.clear();
    std::io::stdin()
        .lock()
        .take(32 * 1024 * 1024 + 1)
        .read_to_end(&mut raw)?;
    ensure(raw.len() <= 32 * 1024 * 1024, "command too large")?;
    let request: Request = serde_json::from_slice(&raw)?;
    ensure(crate::validate_request(&request), "invalid request")?;
    let s = config
        .sessions
        .iter()
        .find(|s| s.id == request.session)
        .ok_or("unknown session")?;
    let read = crate::is_read(&request);
    let db = Database::open_keyed(Path::new(&config.database), read, config.key_env.as_deref())?;
    if !read {
        Knowledge::initialize(&db)?;
    }
    let response = respond_bounded(&db, s, &request, read, 32 * 1024 * 1024)?;
    println!("{}", canonical(&response, false)?);
    Ok(())
}

fn catalogue_schema() -> &'static Value {
    static SCHEMA: std::sync::OnceLock<Value> = std::sync::OnceLock::new();
    SCHEMA
        .get_or_init(|| json(include_str!("catalogue.json")).expect("valid bundled tool catalogue"))
}

fn normalise(a: &Value) -> Result<(String, Value)> {
    keys(a, &["name", "arguments"], &["_meta"])?;
    let catalogue = catalogue_schema();
    let mut name = field(a, "name")?.to_owned();
    let mut args = a["arguments"].clone();
    if name == "memory" {
        validate(&args, &catalogue["compact"]["inputSchema"])?;
        let actions = [
            ("recall", "query"),
            ("propose", "path"),
            ("review", "id"),
            ("accept", "id"),
            ("reject", "id"),
            ("freshness", "id"),
            ("relations", "id"),
            ("graph", "id"),
            ("review_forget", "id"),
            ("forget", "id"),
            ("code_context", "symbol"),
        ];
        let selected: Vec<_> = actions
            .iter()
            .filter(|(a, _)| args.get(*a).is_some())
            .collect();
        ensure(selected.len() == 1, "exactly one action required")?;
        let (action, key) = selected[0];
        let value = args.as_object_mut().unwrap().remove(*action).unwrap();
        args[*key] = value;
        name = if *action == "code_context" {
            action.to_string()
        } else {
            format!("memory_{action}")
        };
    }
    let contract = catalogue["named"].get(&name).ok_or("unknown memory tool")?;
    validate(&args, &contract[1])?;
    Ok((name, args))
}
fn validate(a: &Value, contract: &Value) -> Result<()> {
    let map = a.as_object().ok_or("invalid arguments")?;
    let props = contract["properties"].as_object().ok_or("invalid schema")?;
    ensure(
        contract["required"]
            .as_array()
            .ok_or("invalid schema")?
            .iter()
            .all(|k| map.contains_key(k.as_str().unwrap()))
            && map.keys().all(|k| props.contains_key(k)),
        "invalid arguments",
    )?;
    for (key, value) in map {
        let rule = &props[key];
        ensure(
            value.is_string() && value.as_str().unwrap().chars().count() <= 4096,
            "invalid argument type",
        )?;
        if let Some(options) = rule.get("enum") {
            ensure(
                options.as_array().unwrap().contains(value),
                "unsupported argument value",
            )?;
        }
    }
    checked(a)
}
fn allow_tool(s: &Session, name: &str) -> bool {
    match name {
        "memory_propose" | "memory_accept" => s.generate_memories,
        "memory_reject" | "memory_forget" | "memory_review_forget" => true,
        _ => s.use_memories,
    }
}
fn tool(k: &Knowledge, name: &str, a: &Value) -> Result<Value> {
    match name {
        "memory_recall" => k.recall_compact(field(a, "query")?, text(a, "mode", "lexical")?, 1240),
        "memory_propose" => k.propose(field(a, "path")?),
        "memory_review" => k.review(field(a, "id")?),
        "memory_accept" => k.accept(
            field(a, "id")?,
            field(a, "review_digest")?,
            a.get("supersedes").and_then(Value::as_str),
        ),
        "memory_reject" => k.reject(field(a, "id")?, field(a, "review_digest")?),
        "memory_freshness" => k.freshness(field(a, "id")?),
        "memory_review_forget" => forget_review(k, field(a, "id")?),
        "memory_forget" => {
            let id = field(a, "id")?;
            ensure(
                forget_review(k, id)?["review_digest"] == a["review_digest"],
                "forget review stale",
            )?;
            k.db.conn.execute(
                "UPDATE cortex_memory SET status=2,updated_at=? WHERE memory_id=? AND scope=?",
                params![now(), identifier(id)?, k.session.scope],
            )?;
            k.db.seal("cortex_memory", "memory_id", &identifier(id)?)?;
            Ok(j!({"memory_id":id,"status":"deleted"}))
        }
        "memory_relations" => relations(k, field(a, "id")?),
        "memory_graph" => super::graph::recall(
            k,
            field(a, "id")?,
            text(a, "intent", "related")?,
            text(a, "depth", "2")?.parse()?,
            1100,
        ),
        "code_context" => Err("provider is not configured by the host".into()),
        _ => Err("unsupported memory operation".into()),
    }
}
fn forget_review(k: &Knowledge, id: &str) -> Result<Value> {
    let (row, _) = k.db.memory(&k.session.scope, id, true)?;
    let vault: String = k.db.conn.query_row(
        "SELECT vault_id FROM vault_state WHERE singleton=1",
        [],
        |r| r.get(0),
    )?;
    let body = j!({"scope":k.session.scope,"id":id,"checksum":hex::encode(row.bytes("record_checksum")?),"vault_id":vault});
    Ok(j!({"id":id,"review_digest":digest(&body)?,"operation":"forget"}))
}
fn relations(k: &Knowledge, id: &str) -> Result<Value> {
    k.db.memory(&k.session.scope, id, true)?;
    let mut edges = Vec::new();
    for mut edge in k.related_items(id)? {
        if edge["owner"] != id && edge["target"] != id {
            continue;
        }
        let owner = k.freshness(field(&edge, "owner")?);
        let target = k.freshness(field(&edge, "target")?);
        if let (Ok(owner), Ok(target)) = (owner, target) {
            if owner["state"] == "stale" || target["state"] == "stale" {
                continue;
            }
            edge["endpoint_freshness"] = j!({"owner":owner["state"],"target":target["state"]});
            edges.push(edge);
        }
    }
    Ok(j!({"edges":edges,"inferred_truth":false}))
}
fn background(k: &Knowledge, a: &Value) -> Result<Value> {
    let clock = chrono::Utc::now().timestamp_micros() as f64 / 1_000_000.0;
    let decision = super::generation::evaluate(k.session, &a["chat"], &a["quota"], clock);
    let mut result = decision.receipt();
    if decision.eligible() {
        result["proposal"] = k.propose(field(a, "path")?)?;
        result["generated"] = j!(true);
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cached_catalogue_preserves_compact_validation() {
        assert!(std::ptr::eq(catalogue_schema(), catalogue_schema()));
        let (name, args) =
            normalise(&j!({"name":"memory","arguments":{"recall":"synthetic release"}})).unwrap();
        assert_eq!(name, "memory_recall");
        assert_eq!(args, j!({"query":"synthetic release"}));
        assert!(
            normalise(
                &j!({"name":"memory","arguments":{"recall":"synthetic","propose":"file.md"}})
            )
            .is_err()
        );
        assert!(normalise(&j!({"name":"memory","arguments":{"recall":7}})).is_err());
    }
    #[test]
    fn knowledge_growth_pages_and_revision_guards() {
        let db = Database::initialize(rusqlite::Connection::open_in_memory().unwrap()).unwrap();
        Knowledge::initialize(&db).unwrap();
        let s: Session = serde_json::from_value(
            j!({"id":"chat","scope":"synthetic","source_root":"unused","allow_admin":true}),
        )
        .unwrap();
        let k = Knowledge::new(&db, &s).unwrap();
        db.conn.execute_batch("BEGIN IMMEDIATE").unwrap();
        for n in 0..21 {
            let m = db.remember(&s.scope,&j!({"type":"semantic","subject":format!("Synthetic subject {n}"),"summary":format!("Synthetic fact {n}.")})).unwrap();
            for path in 0..100 {
                k.put(
                    "source",
                    j!({"path":format!("source-{path}.md"),"sha256":"0".repeat(64)}),
                    m["memory_id"].as_str(),
                    None,
                )
                .unwrap();
            }
        }
        db.conn.execute_batch("COMMIT").unwrap();
        k.verify_items().unwrap();
        let mut r: Request = serde_json::from_value(j!({"session":"chat","id":"page","operation":"admin","arguments":{"action":"knowledge-page","arguments":{"limit":200}}})).unwrap();
        let mut total = 0;
        loop {
            let page = respond(&db, &s, &r, true).unwrap()["result"].clone();
            total += page["items"].as_array().unwrap().len();
            if page["next_after"].is_null() {
                break;
            }
            r.arguments["arguments"]["after"] = page["next_after"].clone();
            r.arguments["arguments"]["revision"] = page["revision"].clone();
        }
        assert_eq!(total, 2100);
        db.remember(&s.scope,&j!({"type":"semantic","subject":"New synthetic subject","summary":"New synthetic fact."})).unwrap();
        assert!(respond(&db, &s, &r, true).is_err());
        assert!(k.page("../outside", 100).is_err());
    }
    #[test]
    fn recovery_is_atomic_and_rejects_collisions_and_expiry() {
        let db = Database::initialize(rusqlite::Connection::open_in_memory().unwrap()).unwrap();
        Knowledge::initialize(&db).unwrap();
        let mut s: Session = serde_json::from_value(
            j!({"id":"chat","scope":"synthetic","source_root":"unused","allow_admin":true}),
        )
        .unwrap();
        let mut r: Request = serde_json::from_value(j!({"session":"chat","id":"retry-request","operation":"admin","arguments":{"action":"stage","arguments":{"text":"Synthetic retry body"}},"recovery":{"key":"unique-write","expires_at":chrono::Utc::now().timestamp()+3600}})).unwrap();
        assert!(respond_bounded(&db, &s, &r, false, 16).is_err());
        let first = respond(&db, &s, &r, false).unwrap();
        let replay = respond(&db, &s, &r, false).unwrap();
        for key in ["session", "id", "result"] {
            assert_eq!(first[key], replay[key]);
        }
        assert!(first["native_timing"].is_object());
        assert!(replay["native_timing"].is_object());
        let payload_bytes = canonical(
            &j!({"session":s.id,"id":r.id,"result":first["result"]}),
            false,
        )
        .unwrap()
        .len();
        assert!(respond_bounded(&db, &s, &r, false, payload_bytes + 511).is_err());
        assert_eq!(
            db.conn
                .query_row("SELECT count(*) FROM hippocampus_stage", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            1
        );
        r.arguments["arguments"]["text"] = j!("A conflicting write");
        assert!(respond(&db, &s, &r, false).is_err());
        r.arguments["arguments"]["text"] = j!("Synthetic retry body");
        s.allow_admin = false;
        assert!(respond(&db, &s, &r, false).is_err());
        s.allow_admin = true;
        r.recovery.as_mut().unwrap().expires_at = chrono::Utc::now().timestamp() - 1;
        assert!(respond(&db, &s, &r, false).is_err());
        assert_eq!(
            db.conn
                .query_row("SELECT count(*) FROM hippocampus_stage", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            1
        );
    }
    #[test]
    fn transport_budget_rolls_back_complete_mutation() {
        let db = Database::initialize(rusqlite::Connection::open_in_memory().unwrap()).unwrap();
        Knowledge::initialize(&db).unwrap();
        let s: Session = serde_json::from_value(
            j!({"id":"chat","scope":"synthetic","source_root":"unused","allow_admin":true}),
        )
        .unwrap();
        let r:Request=serde_json::from_value(j!({"session":"chat","id":"request","operation":"admin","arguments":{"action":"remember","arguments":{"type":"semantic","subject":"Synthetic","summary":"A complete synthetic fact."}}})).unwrap();
        let revision: i64 = db
            .conn
            .query_row("SELECT revision FROM vault_state", [], |r| r.get(0))
            .unwrap();
        assert!(respond_bounded(&db, &s, &r, false, 16).is_err());
        assert_eq!(
            db.conn
                .query_row("SELECT count(*) FROM cortex_memory", [], |r| r
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert_eq!(
            db.conn
                .query_row("SELECT revision FROM vault_state", [], |r| r
                    .get::<_, i64>(0))
                .unwrap(),
            revision
        );
        assert!(respond(&db, &s, &r, false).is_ok());
    }
}
