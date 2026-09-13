use super::{
    Result, canonical, codec,
    database::{Database, Row, field, identifier, sql_json},
    ensure, exact,
    knowledge::{Knowledge, checked, digest, keys},
    policy, sha, timestamp,
};
use base64::Engine;
use rusqlite::{params, types::Value as Sql};
use serde_json::{Value, json as j};
use std::collections::{BTreeMap, BTreeSet};

const TABLES: [(&str, &str); 5] = [
    ("cortex_memory", "memory_id"),
    ("cortex_detail", "memory_id"),
    ("cortex_verbatim", "archive_id"),
    ("hippocampus_stage", "stage_id"),
    ("cortex_tombstone", "memory_id"),
];
const MAX_PACKAGE: usize = 16 * 1024 * 1024;
type MemoryId = [u8; 16];
struct Lineage {
    claim: MemoryId,
    supersedes: Option<MemoryId>,
    prior: Option<MemoryId>,
    stage: Option<MemoryId>,
}
fn lineage_id(row: &Row, key: &str) -> Result<Option<MemoryId>> {
    match row.0.get(key) {
        Some(Sql::Null) => Ok(None),
        Some(Sql::Blob(bytes)) => Ok(Some(
            bytes
                .as_slice()
                .try_into()
                .map_err(|_| "invalid stored lineage identity")?,
        )),
        _ => Err("invalid stored lineage identity".into()),
    }
}
fn signed(mut v: Value, ascii: bool) -> Result<Value> {
    v["sha256"] = j!(hex::encode(sha(canonical(&v, ascii)?.as_bytes())));
    Ok(v)
}
fn envelope(v: &Value, format: &str, scope: &str, fields: &[&str], ascii: bool) -> Result<()> {
    keys(v, fields, &[])?;
    ensure(
        v["format"] == format && v["scope"] == scope,
        "unsupported package or scope",
    )?;
    let mut body = v.clone();
    body.as_object_mut().unwrap().remove("sha256");
    let raw = canonical(&body, ascii)?;
    ensure(
        raw.len() <= MAX_PACKAGE && v["sha256"] == hex::encode(sha(raw.as_bytes())),
        "package integrity failure",
    )
}
impl Database {
    pub fn verify_scope(&self, scope: &str) -> Result<()> {
        policy::check_text(scope, 256)?;
        ensure(!scope.trim().is_empty(), "scope required")?;
        ensure(
            self.rows("PRAGMA foreign_key_check", vec![])?.is_empty(),
            "unresolved foreign keys",
        )?;
        let mut memories = BTreeMap::new();
        let mut active = BTreeSet::new();
        for (table, key) in TABLES {
            if table == "cortex_detail" {
                continue;
            }
            let mut after = None;
            loop {
                let (clause, values) = match after {
                    Some(cursor) => (
                        format!(" AND {key}>?"),
                        vec![Sql::Text(scope.into()), Sql::Blob(cursor)],
                    ),
                    None => (String::new(), vec![Sql::Text(scope.into())]),
                };
                let rows = self.rows(
                    &format!(
                        "SELECT * FROM {table} WHERE scope=?{clause} ORDER BY {key} LIMIT 512"
                    ),
                    values,
                )?;
                if rows.is_empty() {
                    break;
                }
                after = Some(rows.last().unwrap().bytes(key)?.to_vec());
                for row in rows {
                    ensure(row.bytes(key)?.len() == 16, "invalid stored identity")?;
                    for key in [
                        "created_at",
                        "updated_at",
                        "observed_at",
                        "valid_from",
                        "removed_at",
                        "expires_at",
                        "valid_to",
                    ] {
                        if row.0.contains_key(key) {
                            let value = row.optional(key)?;
                            ensure(
                                value.is_some() || ["expires_at", "valid_to"].contains(&key),
                                "required timestamp",
                            )?;
                            if let Some(s) = value {
                                ensure(timestamp(s)? == s, "noncanonical timestamp")?;
                            }
                        }
                    }
                    if row.0.contains_key("status") {
                        ensure(
                            (0..=if table == "hippocampus_stage" { 2 } else { 4 })
                                .contains(&row.int("status")?),
                            "invalid status",
                        )?;
                    }
                    if row.0.contains_key("pinned") {
                        ensure([0, 1].contains(&row.int("pinned")?), "invalid pin")?;
                    }
                    if table == "cortex_tombstone" {
                        continue;
                    }
                    ensure(
                        self.conn.query_row(
                            "SELECT count(*) FROM cortex_tombstone WHERE memory_id=?",
                            [row.bytes(key)?],
                            |r| r.get::<_, i64>(0),
                        )? == 0,
                        "purged identifier resurrected",
                    )?;
                    match table {
                        "cortex_memory" => {
                            let payload = self.verify_memory(&row)?;
                            let detail = self.detail(&row)?;
                            let raw = codec::decompress(row.bytes("payload_blob")?)?;
                            ensure(
                                (1..=5).contains(&row.int("memory_type")?)
                                    && (0..=3).contains(&row.int("sensitivity")?)
                                    && payload[2].is_empty(),
                                "invalid payload layout",
                            )?;
                            for k in ["importance", "confidence"] {
                                ensure((0..=255).contains(&row.int(k)?), "invalid quality")?;
                            }
                            let mut bytes =
                                vec![row.int("memory_type")? as u8, row.int("sensitivity")? as u8];
                            bytes.extend_from_slice(scope.as_bytes());
                            bytes.extend_from_slice(&raw);
                            bytes.extend_from_slice(&sha(detail.as_bytes()));
                            ensure(
                                sha(&bytes) == row.bytes("content_fingerprint")?
                                    && row.int("payload_raw_bytes")? == raw.len() as i64
                                    && row.int("payload_stored_bytes")?
                                        == row.bytes("payload_blob")?.len() as i64,
                                "semantic fingerprint or size failure",
                            )?;
                            ensure(
                                row.optional("valid_to")?
                                    .is_none_or(|v| v > row.text("valid_from").unwrap_or("")),
                                "invalid validity",
                            )?;
                            if let Some(s) = row.optional("source_hash")? {
                                ensure(
                                    s.len() == 64
                                        && hex::decode(s).is_ok()
                                        && s.to_lowercase() == s,
                                    "invalid source hash",
                                )?;
                            }
                            policy::check_text(row.text("confidence_reason")?, policy::MAX_TEXT)?;
                            let claim: MemoryId = identifier(row.text("claim_id")?)?
                                .try_into()
                                .map_err(|_| "invalid stored claim identity")?;
                            if row.int("status")? == 0 {
                                ensure(active.insert(claim), "multiple active claim versions")?;
                            }
                            // Payloads and other checked metadata are no longer
                            // needed by the cross-page correction graph.
                            let id: MemoryId = row
                                .bytes(key)?
                                .try_into()
                                .map_err(|_| "invalid stored memory identity")?;
                            memories.insert(
                                id,
                                Lineage {
                                    claim,
                                    supersedes: lineage_id(&row, "supersedes_id")?,
                                    prior: lineage_id(&row, "prior_version_id")?,
                                    stage: lineage_id(&row, "stage_id")?,
                                },
                            );
                        }
                        "cortex_verbatim" => {
                            exact::verify(&row)?;
                            if let Some(Sql::Blob(b)) = row.0.get("linked_memory_id") {
                                self.memory(scope, &hex::encode(b), false)?;
                            }
                        }
                        "hippocampus_stage" => {
                            row.verify()?;
                            let raw = codec::decompress(row.bytes("raw_blob")?)?;
                            policy::check_text(std::str::from_utf8(&raw)?, policy::MAX_TEXT)?;
                            policy::check_text(row.text("source")?, policy::MAX_TEXT)?;
                            ensure(
                                row.optional("expires_at")?.is_some()
                                    && sha(&raw) == row.bytes("checksum_sha256")?
                                    && row.int("raw_bytes")? == raw.len() as i64
                                    && row.int("stored_bytes")?
                                        == row.bytes("raw_blob")?.len() as i64,
                                "stage integrity failure",
                            )?;
                            ensure(
                                row.int("status")? == 0
                                    || (raw.is_empty() && row.bytes("raw_blob")?.is_empty()),
                                "disposed stage content",
                            )?;
                        }
                        _ => unreachable!(),
                    }
                }
            }
        }
        let mut verified_lineage = BTreeSet::new();
        for (id, row) in &memories {
            let mut seen = BTreeSet::new();
            let mut current = id;
            while !verified_lineage.contains(current)
                && let Some(parent) = &memories
                    .get(current)
                    .ok_or("cross-scope correction")?
                    .supersedes
            {
                ensure(seen.insert(*current), "cyclic corrections")?;
                let parent_row = memories.get(parent).ok_or("unresolved correction")?;
                ensure(parent_row.claim == row.claim, "claim disagreement")?;
                current = parent;
            }
            verified_lineage.extend(seen);
            verified_lineage.insert(*current);
            if let Some(parent) = row.supersedes {
                ensure(row.prior == Some(parent), "correction history disagreement")?;
            } else if let Some(parent) = row.prior {
                ensure(
                    self.conn.query_row(
                        "SELECT count(*) FROM cortex_tombstone WHERE memory_id=? AND scope=?",
                        params![parent.as_slice(), scope],
                        |r| r.get::<_, i64>(0),
                    )? == 1,
                    "missing correction tombstone",
                )?;
            }
            if let Some(stage) = row.stage {
                let rows = self.rows(
                    "SELECT scope,status FROM hippocampus_stage WHERE stage_id=?",
                    vec![Sql::Blob(stage.to_vec())],
                )?;
                if let Some(r) = rows.first() {
                    ensure(
                        r.text("scope")? == scope && r.int("status")? == 1,
                        "stage provenance disagreement",
                    )?;
                } else {
                    ensure(
                        self.conn.query_row(
                            "SELECT count(*) FROM cortex_tombstone WHERE memory_id=? AND scope=?",
                            params![stage.as_slice(), scope],
                            |r| r.get::<_, i64>(0),
                        )? == 1,
                        "missing stage provenance",
                    )?;
                }
            }
        }
        Ok(())
    }
    pub fn export_scope(&self, scope: &str) -> Result<Value> {
        self.verify_scope(scope)?;
        let mut tables = serde_json::Map::new();
        for (table, key) in TABLES {
            let sql = if table == "cortex_detail" {
                "SELECT d.* FROM cortex_detail d JOIN cortex_memory m USING(memory_id) WHERE m.scope=? ORDER BY d.memory_id".into()
            } else {
                format!("SELECT * FROM {table} WHERE scope=? ORDER BY {key}")
            };
            let mut rows = Vec::new();
            let mut bytes = 0;
            for row in self.rows(&sql, vec![Sql::Text(scope.into())])? {
                let mut map = serde_json::Map::new();
                for (k, v) in row.0 {
                    if k != "memory_pk" {
                        map.insert(k, sql_json(&v, true)?);
                    }
                }
                let value = Value::Object(map);
                bytes += canonical(&value, true)?.len();
                ensure(bytes <= MAX_PACKAGE, "export size limit")?;
                rows.push(value);
            }
            tables.insert(table.into(), j!(rows));
        }
        let body = j!({"format":"memorycore-ai-scope/2","scope":scope,"tables":tables});
        ensure(
            canonical(&body, true)?.len() <= MAX_PACKAGE,
            "export size limit",
        )?;
        signed(body, true)
    }
    pub fn import_scope(&self, scope: &str, package: &Value) -> Result<Value> {
        envelope(
            package,
            "memorycore-ai-scope/2",
            scope,
            &["format", "scope", "tables", "sha256"],
            true,
        )?;
        keys(&package["tables"], &TABLES.map(|(t, _)| t), &[])?;
        self.conn.execute_batch("PRAGMA defer_foreign_keys=ON")?;
        for (table, key) in TABLES {
            let columns: BTreeSet<String> = self
                .rows(&format!("PRAGMA table_info({table})"), vec![])?
                .iter()
                .map(|r| r.text("name").map(String::from))
                .collect::<Result<_>>()?;
            let columns: BTreeSet<_> = columns.into_iter().filter(|c| c != "memory_pk").collect();
            for item in package["tables"][table]
                .as_array()
                .ok_or("invalid table rows")?
            {
                let map = item.as_object().ok_or("invalid row")?;
                ensure(
                    map.keys().cloned().collect::<BTreeSet<_>>() == columns,
                    "unexpected row fields",
                )?;
                let mut row = BTreeMap::new();
                for (k, v) in map {
                    let value = match v {
                        Value::Null => Sql::Null,
                        Value::String(s) => Sql::Text(s.clone()),
                        Value::Number(n) => {
                            Sql::Integer(n.as_i64().ok_or("invalid stored integer")?)
                        }
                        Value::Object(_) => {
                            keys(v, &["base64"], &[])?;
                            Sql::Blob(
                                base64::engine::general_purpose::STANDARD
                                    .decode(field(v, "base64")?)?,
                            )
                        }
                        _ => return Err("invalid portable field".into()),
                    };
                    row.insert(k.clone(), value);
                }
                let r = Row(row);
                let id = r.bytes(key)?;
                ensure(id.len() == 16, "invalid row identity")?;
                if table == "cortex_detail" {
                    self.memory(scope, &hex::encode(id), false)?;
                } else {
                    ensure(r.text("scope")? == scope, "cross-scope row")?;
                }
                if table != "cortex_tombstone" {
                    ensure(
                        self.conn.query_row(
                            "SELECT count(*) FROM cortex_tombstone WHERE memory_id=?",
                            [id],
                            |r| r.get::<_, i64>(0),
                        )? == 0,
                        "purged identifier",
                    )?;
                }
                self.insert(table, r.0)?;
            }
        }
        self.verify_scope(scope)?;
        for row in self.rows(
            "SELECT memory_id FROM cortex_memory WHERE scope=?",
            vec![Sql::Text(scope.into())],
        )? {
            self.index(row.bytes("memory_id")?)?;
        }
        Ok(j!({"scope":scope,"imported":true,"sha256":package["sha256"]}))
    }
}
impl Knowledge<'_> {
    pub fn export(&self, allow_routing: bool) -> Result<Value> {
        if !allow_routing
            && self.db.conn.query_row(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='session_route'",
                [],
                |r| r.get::<_, i64>(0),
            )? > 0
        {
            ensure(
                self.db.conn.query_row(
                    "SELECT count(*) FROM session_route WHERE scope=?",
                    [&self.session.scope],
                    |r| r.get::<_, i64>(0),
                )? == 0,
                "routing audit export required",
            )?;
        }
        signed(
            j!({"format":"brain-knowledge/1","scope":self.session.scope,"core":self.db.export_scope(&self.session.scope)?,"items":self.items(None)?}),
            false,
        )
    }
    pub fn import(&self, package: &Value, reviewed: bool) -> Result<Value> {
        ensure(reviewed, "reviewed import required")?;
        checked(package)?;
        envelope(
            package,
            "brain-knowledge/1",
            &self.session.scope,
            &["format", "scope", "core", "items", "sha256"],
            false,
        )?;
        let items = package["items"]
            .as_array()
            .ok_or("invalid knowledge items")?;
        ensure(items.len() <= 2000, "knowledge item limit")?;
        self.db
            .import_scope(&self.session.scope, &package["core"])?;
        for item in items {
            self.validate(item)?;
            self.db.conn.execute(
                "INSERT INTO knowledge_item VALUES(?,?,?,?,?,?,?)",
                params![
                    field(item, "id")?,
                    self.session.scope,
                    field(item, "kind")?,
                    item["owner"].as_str().map(identifier).transpose()?,
                    item["target"].as_str().map(identifier).transpose()?,
                    canonical(&item["payload"], false)?,
                    digest(item)?
                ],
            )?;
        }
        self.verify_items()?;
        Ok(j!({"imported":true,"source_freshness":"must_be_rechecked_in_selected_root"}))
    }
}

#[cfg(test)]
mod verification_tests {
    use super::*;
    #[test]
    fn lineage_retention_is_fixed_size_and_rejects_malformed_ids() {
        assert!(!std::mem::needs_drop::<Lineage>());
        assert!(std::mem::size_of::<Lineage>() <= 80);
        for value in [
            Sql::Blob(vec![0; 15]),
            Sql::Text("00".repeat(16)),
            Sql::Integer(1),
        ] {
            let row = Row(BTreeMap::from([("prior_version_id".into(), value)]));
            assert!(lineage_id(&row, "prior_version_id").is_err());
        }
        let row = Row(BTreeMap::from([("prior_version_id".into(), Sql::Null)]));
        assert_eq!(lineage_id(&row, "prior_version_id").unwrap(), None);
        let row = Row(BTreeMap::from([(
            "prior_version_id".into(),
            Sql::Blob(vec![7; 16]),
        )]));
        assert_eq!(lineage_id(&row, "prior_version_id").unwrap(), Some([7; 16]));
    }

    #[test]
    fn compact_lineage_preserves_cross_page_cycle_claim_and_history_checks() {
        let db = Database::initialize(rusqlite::Connection::open_in_memory().unwrap()).unwrap();
        Knowledge::initialize(&db).unwrap();
        let scope = "synthetic:lineage";
        let mut ids = Vec::new();
        for n in 0..520 {
            let mut args = j!({"type":"semantic","subject":"Synthetic lineage",
                "summary":format!("Synthetic revision {n}.")});
            if let Some(prior) = ids.last() {
                args["supersedes"] = j!(hex::encode(prior));
            }
            let saved = db.remember(scope, &args).unwrap();
            ids.push(identifier(saved["memory_id"].as_str().unwrap()).unwrap());
        }
        db.verify_scope(scope).unwrap();
        let root = &ids[0];
        let tail = ids.last().unwrap();
        db.conn
            .execute(
                "UPDATE cortex_memory SET supersedes_id=?,prior_version_id=? WHERE memory_id=?",
                params![tail, tail, root],
            )
            .unwrap();
        db.seal("cortex_memory", "memory_id", root).unwrap();
        assert!(
            db.verify_scope(scope)
                .unwrap_err()
                .to_string()
                .contains("cyclic corrections")
        );
        db.conn.execute("UPDATE cortex_memory SET supersedes_id=NULL,prior_version_id=NULL WHERE memory_id=?", [root]).unwrap();
        db.seal("cortex_memory", "memory_id", root).unwrap();
        db.verify_scope(scope).unwrap();

        db.conn
            .execute(
                "UPDATE cortex_memory SET prior_version_id=? WHERE memory_id=?",
                params![root, tail],
            )
            .unwrap();
        db.seal("cortex_memory", "memory_id", tail).unwrap();
        assert!(
            db.verify_scope(scope)
                .unwrap_err()
                .to_string()
                .contains("correction history disagreement")
        );
        db.conn
            .execute(
                "UPDATE cortex_memory SET prior_version_id=supersedes_id WHERE memory_id=?",
                [tail],
            )
            .unwrap();
        db.seal("cortex_memory", "memory_id", tail).unwrap();

        let claim = db
            .memory(scope, &hex::encode(root), false)
            .unwrap()
            .0
            .text("claim_id")
            .unwrap()
            .to_owned();
        db.conn
            .execute(
                "UPDATE cortex_memory SET claim_id=? WHERE memory_id=?",
                params![uuid::Uuid::new_v4().simple().to_string(), root],
            )
            .unwrap();
        db.seal("cortex_memory", "memory_id", root).unwrap();
        assert!(
            db.verify_scope(scope)
                .unwrap_err()
                .to_string()
                .contains("claim disagreement")
        );
        db.conn
            .execute(
                "UPDATE cortex_memory SET claim_id=? WHERE memory_id=?",
                params![claim, root],
            )
            .unwrap();
        db.seal("cortex_memory", "memory_id", root).unwrap();

        let outside = db
            .remember(
                "synthetic:other-lineage",
                &j!({"type":"semantic","subject":"Independent",
            "summary":"Separate scope."}),
            )
            .unwrap();
        let outside = identifier(outside["memory_id"].as_str().unwrap()).unwrap();
        db.conn
            .execute(
                "UPDATE cortex_memory SET supersedes_id=?,prior_version_id=? WHERE memory_id=?",
                params![outside, outside, root],
            )
            .unwrap();
        db.seal("cortex_memory", "memory_id", root).unwrap();
        assert!(
            db.verify_scope(scope)
                .unwrap_err()
                .to_string()
                .contains("unresolved correction")
        );
    }

    #[test]
    fn keyset_verification_checks_later_pages_and_empty_identities() {
        let db = Database::initialize(rusqlite::Connection::open_in_memory().unwrap()).unwrap();
        let scope = "synthetic:paging";
        for n in 0u128..513 {
            db.conn
                .execute(
                    "INSERT INTO cortex_tombstone VALUES(?,?,?)",
                    params![n.to_be_bytes().to_vec(), scope, super::super::now()],
                )
                .unwrap();
        }
        db.verify_scope(scope).unwrap();
        db.conn
            .execute(
                "UPDATE cortex_tombstone SET removed_at='invalid' WHERE memory_id=?",
                [512u128.to_be_bytes().to_vec()],
            )
            .unwrap();
        assert!(db.verify_scope(scope).is_err());
        db.conn
            .execute(
                "UPDATE cortex_tombstone SET removed_at=?",
                [super::super::now()],
            )
            .unwrap();
        db.verify_scope(scope).unwrap();
        db.conn
            .execute(
                "INSERT INTO cortex_tombstone VALUES(?,?,?)",
                params![Vec::<u8>::new(), scope, super::super::now()],
            )
            .unwrap();
        assert!(db.verify_scope(scope).is_err());
    }
}
