use super::{Result, after_days, canonical, codec, ensure, now, policy, sha, timestamp};
use rusqlite::{Connection, OpenFlags, params, params_from_iter, types::Value as Sql};
use serde_json::{Value, json};
use std::{collections::BTreeMap, path::Path, time::Duration};

#[derive(Clone, Debug)]
pub struct Row(pub BTreeMap<String, Sql>);
impl Row {
    pub fn text(&self, k: &str) -> Result<&str> {
        match self.0.get(k) {
            Some(Sql::Text(v)) => Ok(v),
            _ => Err("invalid stored text".into()),
        }
    }
    pub fn bytes(&self, k: &str) -> Result<&[u8]> {
        match self.0.get(k) {
            Some(Sql::Blob(v)) => Ok(v),
            _ => Err("invalid stored bytes".into()),
        }
    }
    pub fn int(&self, k: &str) -> Result<i64> {
        match self.0.get(k) {
            Some(Sql::Integer(v)) => Ok(*v),
            _ => Err("invalid stored integer".into()),
        }
    }
    pub fn optional(&self, k: &str) -> Result<Option<&str>> {
        if self.0.get(k) == Some(&Sql::Null) {
            Ok(None)
        } else {
            Ok(Some(self.text(k)?))
        }
    }
    pub fn value(&self, k: &str) -> Result<Value> {
        sql_json(self.0.get(k).ok_or("missing stored field")?, false)
    }
    pub fn digest(&self) -> Result<Vec<u8>> {
        let mut map = serde_json::Map::new();
        for (k, v) in &self.0 {
            if !["record_checksum", "memory_pk", "hits"].contains(&k.as_str()) {
                map.insert(k.clone(), sql_json(v, false)?);
            }
        }
        Ok(sha(canonical(&Value::Object(map), true)?.as_bytes()))
    }
    pub fn verify(&self) -> Result<()> {
        ensure(
            self.bytes("record_checksum")? == self.digest()?,
            "record metadata integrity failure",
        )
    }
}

pub fn sql_json(v: &Sql, portable: bool) -> Result<Value> {
    use base64::Engine;
    Ok(match v {
        Sql::Null => Value::Null,
        Sql::Integer(n) => json!(n),
        Sql::Real(n) => {
            ensure(n.is_finite(), "invalid stored number")?;
            json!(n)
        }
        Sql::Text(s) => json!(s),
        Sql::Blob(b) => {
            if portable {
                json!({"base64":base64::engine::general_purpose::STANDARD.encode(b)})
            } else {
                json!({"bytes":hex::encode(b)})
            }
        }
    })
}

pub fn identifier(value: &str) -> Result<Vec<u8>> {
    let bytes = hex::decode(value)?;
    ensure(
        bytes.len() == 16 && value == hex::encode(&bytes),
        "invalid identifier",
    )?;
    Ok(bytes)
}
pub fn field<'a>(v: &'a Value, k: &str) -> Result<&'a str> {
    v.get(k)
        .and_then(Value::as_str)
        .ok_or_else(|| "required text field".into())
}
pub fn text<'a>(v: &'a Value, k: &str, default: &'a str) -> Result<&'a str> {
    match v.get(k) {
        None | Some(Value::Null) => Ok(default),
        Some(Value::String(s)) => Ok(s),
        _ => Err("invalid text field".into()),
    }
}
pub fn boolean(v: &Value, k: &str, default: bool) -> Result<bool> {
    match v.get(k) {
        None => Ok(default),
        Some(Value::Bool(b)) => Ok(*b),
        _ => Err("invalid boolean".into()),
    }
}
pub fn optional_time(v: &Value, k: &str) -> Result<Option<String>> {
    match v.get(k) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) => Ok(Some(timestamp(s)?)),
        _ => Err("invalid timestamp".into()),
    }
}
fn sql_time(value: &Option<String>) -> Sql {
    value
        .as_ref()
        .map(|s| Sql::Text(s.clone()))
        .unwrap_or(Sql::Null)
}
pub fn quality(v: &Value, k: &str, default: f64) -> Result<i64> {
    let n = match v.get(k) {
        None => default,
        Some(n) => n.as_f64().ok_or("invalid quality")?,
    };
    ensure(n.is_finite() && (0.0..=1.0).contains(&n), "invalid quality")?;
    Ok((n * 255.0).round_ties_even() as i64)
}

pub struct Database {
    pub conn: Connection,
}

pub struct PreparedMemory {
    args_digest: Vec<u8>,
    payload: Vec<u8>,
    raw: Vec<u8>,
    detail: Vec<u8>,
    terms: Vec<Vec<u8>>,
}
fn configure_page_cache(conn: &Connection, bytes: usize) -> Result<()> {
    ensure(
        (2 * 1024 * 1024..=64 * 1024 * 1024).contains(&bytes),
        "invalid SQLite page cache budget",
    )?;
    conn.pragma_update(None, "cache_size", -((bytes / 1024) as i64))?;
    Ok(())
}
impl Database {
    pub fn open(path: &Path, read_only: bool) -> Result<Self> {
        Self::open_keyed(path, read_only, None)
    }
    pub fn open_keyed(path: &Path, read_only: bool, key_env: Option<&str>) -> Result<Self> {
        ensure(path.is_file(), "database must exist")?;
        let conn = Connection::open_with_flags(
            path,
            if read_only {
                OpenFlags::SQLITE_OPEN_READ_ONLY
            } else {
                OpenFlags::SQLITE_OPEN_READ_WRITE
            },
        )?;
        Self::unlock(&conn, key_env)?;
        conn.busy_timeout(Duration::from_secs(5))?;
        conn.execute_batch("PRAGMA foreign_keys=ON; PRAGMA trusted_schema=OFF;")?;
        let cache_bytes = std::env::var("MEMORYCORE_AI_SQLITE_CACHE_BYTES")
            .map_or(Ok(2 * 1024 * 1024), |s| s.parse::<usize>())?;
        configure_page_cache(&conn, cache_bytes)?;
        if !read_only {
            conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA secure_delete=ON;")?;
        }
        ensure(
            conn.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))? == 2,
            "unsupported schema",
        )?;
        Ok(Self { conn })
    }
    pub fn unlock(conn: &Connection, key_env: Option<&str>) -> Result<()> {
        if let Some(name) = key_env {
            ensure(cfg!(feature = "sqlcipher"), "SQLCipher build required")?;
            // SQLCipher's Windows memory-lock warning formatter can allocate
            // through the same secured allocator. Keep error diagnostics, but
            // prevent recursive warning allocation when VirtualLock is limited.
            conn.execute_batch("PRAGMA cipher_log_level=ERROR;")?;
            let version: String = conn.query_row("PRAGMA cipher_version", [], |r| r.get(0))?;
            ensure(!version.is_empty(), "encryption engine unavailable")?;
            let key = zeroize::Zeroizing::new(
                std::env::var(name).map_err(|_| "encryption key unavailable")?,
            );
            ensure(
                key.len() == 64 && key.bytes().all(|b| b.is_ascii_hexdigit()),
                "expected 256-bit hexadecimal key",
            )?;
            let sql = zeroize::Zeroizing::new(format!("PRAGMA key=\"x'{}'\";", key.as_str()));
            conn.execute_batch(sql.as_str())?;
            conn.execute_batch("PRAGMA cipher_memory_security=ON; PRAGMA temp_store=MEMORY;")?;
        }
        Ok(())
    }
    pub fn encrypted(&self) -> bool {
        self.conn
            .query_row("PRAGMA cipher_salt", [], |r| r.get::<_, String>(0))
            .is_ok_and(|s| s.len() == 32)
    }
    pub fn initialize(conn: Connection) -> Result<Self> {
        ensure(
            conn.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))? == 0,
            "initialise only an empty database",
        )?;
        ensure(
            conn.query_row(
                "SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'",
                [],
                |r| r.get::<_, i64>(0),
            )? == 0,
            "initialise only an empty database",
        )?;
        conn.execute_batch("PRAGMA foreign_keys=ON;BEGIN IMMEDIATE;")?;
        let result = (|| -> Result<()> {
            conn.execute_batch(include_str!("schema.sql"))?;
            conn.execute(
                "INSERT INTO vault_state VALUES(1,?,0)",
                [uuid::Uuid::new_v4().simple().to_string()],
            )?;
            conn.execute_batch("PRAGMA user_version=2;COMMIT;")?;
            Ok(())
        })();
        if result.is_err() {
            let _ = conn.execute_batch("ROLLBACK;");
        }
        result?;
        Ok(Self { conn })
    }
    pub fn rows(&self, sql: &str, values: Vec<Sql>) -> Result<Vec<Row>> {
        let mut statement = self.conn.prepare_cached(sql)?;
        let names: Vec<String> = statement
            .column_names()
            .iter()
            .map(|s| s.to_string())
            .collect();
        let mapped = statement.query_map(params_from_iter(values), |row| {
            let mut map = BTreeMap::new();
            for (i, k) in names.iter().enumerate() {
                map.insert(k.clone(), row.get::<_, Sql>(i)?);
            }
            Ok(Row(map))
        })?;
        let mut rows = Vec::new();
        let mut bytes = 0usize;
        for row in mapped {
            let row = row?;
            for v in row.0.values() {
                bytes += match v {
                    Sql::Text(s) => s.len(),
                    Sql::Blob(b) => b.len(),
                    _ => 8,
                };
            }
            ensure(
                rows.len() < 20_000 && bytes <= 32 * 1024 * 1024,
                "database result exceeds maintenance bound",
            )?;
            rows.push(row);
        }
        Ok(rows)
    }
    pub fn row(&self, table: &str, key: &str, id: &[u8]) -> Result<Row> {
        ensure(
            [
                ("cortex_memory", "memory_id"),
                ("cortex_verbatim", "archive_id"),
                ("hippocampus_stage", "stage_id"),
                ("cortex_detail", "memory_id"),
            ]
            .contains(&(table, key)),
            "invalid internal table",
        )?;
        self.rows(
            &format!("SELECT * FROM {table} WHERE {key}=?"),
            vec![Sql::Blob(id.to_vec())],
        )?
        .into_iter()
        .next()
        .ok_or_else(|| "record not found".into())
    }
    pub fn seal(&self, table: &str, key: &str, id: &[u8]) -> Result<()> {
        let sum = self.row(table, key, id)?.digest()?;
        self.conn.execute(
            &format!("UPDATE {table} SET record_checksum=? WHERE {key}=?"),
            params![sum, id],
        )?;
        Ok(())
    }
    pub fn memory(&self, scope: &str, id: &str, active: bool) -> Result<(Row, [String; 5])> {
        let row = self.row("cortex_memory", "memory_id", &identifier(id)?)?;
        ensure(row.text("scope")? == scope, "scoped memory not found")?;
        let payload = self.verify_memory(&row)?;
        if active {
            let now = now();
            ensure(
                row.int("status")? == 0
                    && row.optional("expires_at")?.is_none_or(|s| s > now.as_str())
                    && row
                        .optional("valid_from")?
                        .is_none_or(|s| s <= now.as_str())
                    && row.optional("valid_to")?.is_none_or(|s| s > now.as_str()),
                "inactive memory",
            )?;
        }
        Ok((row, payload))
    }
    pub fn verify_memory(&self, row: &Row) -> Result<[String; 5]> {
        row.verify()?;
        let raw = codec::decompress(row.bytes("payload_blob")?)?;
        let mut canonical = vec![
            u8::try_from(row.int("memory_type")?)?,
            u8::try_from(row.int("sensitivity")?)?,
        ];
        canonical.extend_from_slice(row.text("scope")?.as_bytes());
        canonical.extend_from_slice(&raw);
        ensure(
            sha(&canonical) == row.bytes("checksum_sha256")?,
            "semantic integrity failure",
        )?;
        let fields = codec::decode(row.bytes("payload_blob")?)?;
        for f in &fields {
            policy::check_text(f, policy::MAX_TEXT)?;
        }
        Ok(fields)
    }
    pub fn detail(&self, row: &Row) -> Result<String> {
        let detail = self.row("cortex_detail", "memory_id", row.bytes("memory_id")?)?;
        let raw = codec::decompress(detail.bytes("detail_blob")?)?;
        ensure(
            sha(&raw) == detail.bytes("checksum_sha256")?
                && sha(&raw) == row.bytes("detail_sha256")?,
            "detail integrity failure",
        )?;
        let result = String::from_utf8(raw)?;
        policy::check_text(&result, policy::MAX_TEXT)?;
        Ok(result)
    }
    pub fn replace_source(
        &self,
        scope: &str,
        id: &str,
        path: &str,
        source_hash: &str,
    ) -> Result<()> {
        let (row, mut fields) = self.memory(scope, id, false)?;
        policy::check_text(path, policy::MAX_TEXT)?;
        fields[4] = path.into();
        let payload = codec::encode([&fields[0], &fields[1], &fields[2], &fields[3], &fields[4]])?;
        let raw = codec::decompress(&payload)?;
        let mut canonical = vec![row.int("memory_type")? as u8, row.int("sensitivity")? as u8];
        canonical.extend_from_slice(scope.as_bytes());
        canonical.extend_from_slice(&raw);
        let checksum = sha(&canonical);
        canonical.extend_from_slice(row.bytes("detail_sha256")?);
        self.conn.execute("UPDATE cortex_memory SET payload_blob=?,payload_raw_bytes=?,payload_stored_bytes=?,checksum_sha256=?,content_fingerprint=?,source_hash=?,updated_at=? WHERE memory_id=? AND scope=?",
            params![payload,raw.len() as i64,payload.len() as i64,checksum,sha(&canonical),source_hash,now(),identifier(id)?,scope])?;
        self.seal("cortex_memory", "memory_id", &identifier(id)?)
    }
    pub fn index(&self, id: &[u8]) -> Result<()> {
        let row = self.row("cortex_memory", "memory_id", id)?;
        let fields = self.verify_memory(&row)?;
        let terms = codec::tokenize(&format!(
            "{} {} {} {}",
            fields[0],
            fields[1],
            fields[3],
            self.detail(&row)?
        ));
        self.conn.execute(
            "DELETE FROM cortex_term WHERE memory_pk=?",
            [row.int("memory_pk")?],
        )?;
        for token in terms {
            self.conn.execute(
                "INSERT INTO cortex_term VALUES(?,?)",
                params![row.int("memory_pk")?, codec::term_hash(&token)],
            )?;
        }
        Ok(())
    }
    pub fn insert(&self, table: &str, values: BTreeMap<String, Sql>) -> Result<()> {
        ensure(
            [
                "cortex_memory",
                "cortex_verbatim",
                "hippocampus_stage",
                "cortex_detail",
                "cortex_tombstone",
            ]
            .contains(&table),
            "invalid internal insert",
        )?;
        let columns = self.rows(&format!("PRAGMA table_info({table})"), vec![])?;
        for key in values.keys() {
            ensure(
                columns
                    .iter()
                    .any(|r| r.text("name").ok() == Some(key.as_str())),
                "unknown stored column",
            )?;
        }
        let keys = values.keys().cloned().collect::<Vec<_>>().join(",");
        let slots = vec!["?"; values.len()].join(",");
        self.conn.execute(
            &format!("INSERT INTO {table} ({keys}) VALUES ({slots})"),
            params_from_iter(values.values()),
        )?;
        Ok(())
    }
    pub fn remember(&self, scope: &str, args: &Value) -> Result<Value> {
        self.remember_prepared(scope, args, &Self::prepare_memory(args)?)
    }
    pub fn prepare_memory(args: &Value) -> Result<PreparedMemory> {
        let subject = field(args, "subject")?.trim();
        let summary = field(args, "summary")?.trim();
        let detail = text(args, "detail", "")?.trim();
        let keywords = text(args, "keywords", "")?;
        let source = text(args, "source", "user")?;
        for value in [
            subject,
            summary,
            detail,
            keywords,
            source,
            text(args, "confidence_reason", "")?,
        ] {
            policy::check_text(value, policy::MAX_TEXT)?;
        }
        let payload = codec::encode([subject, summary, "", keywords, source])?;
        Ok(PreparedMemory {
            args_digest: sha(canonical(args, false)?.as_bytes()),
            raw: codec::decompress(&payload)?,
            payload,
            detail: codec::compress(detail.as_bytes())?,
            terms: codec::tokenize(&format!("{subject} {summary} {keywords} {detail}"))
                .iter()
                .map(|t| codec::term_hash(t))
                .collect(),
        })
    }
    pub fn remember_prepared(
        &self,
        scope: &str,
        args: &Value,
        prepared: &PreparedMemory,
    ) -> Result<Value> {
        ensure(
            prepared.args_digest == sha(canonical(args, false)?.as_bytes()),
            "prepared memory arguments changed",
        )?;
        policy::check_text(scope, 256)?;
        ensure(!scope.trim().is_empty(), "explicit scope required")?;
        let kind = match field(args, "type")? {
            "semantic" => 1,
            "episodic" => 2,
            "procedural" => 3,
            "priming_conditioning" => 4,
            "classical_conditioning" => 5,
            _ => return Err("invalid memory type".into()),
        };
        if kind == 5 {
            ensure(
                boolean(args, "user_confirmed", false)?,
                "explicit conditioning approval required",
            )?;
        }
        let subject = field(args, "subject")?.trim();
        let summary = field(args, "summary")?.trim();
        ensure(!subject.is_empty() && !summary.is_empty(), "empty memory")?;
        let detail = text(args, "detail", "")?.trim();
        let reason = text(args, "confidence_reason", "")?;
        let importance = quality(args, "importance", 0.5)?;
        let confidence = quality(args, "confidence", 1.0)?;
        let sensitivity = match text(args, "sensitivity", "internal")? {
            "public" => 0,
            "internal" => 1,
            "confidential" => 2,
            "restricted" => 3,
            _ => return Err("invalid sensitivity".into()),
        };
        let payload = &prepared.payload;
        let raw = &prepared.raw;
        let detail_sum = sha(detail.as_bytes());
        let mut c = vec![kind as u8, sensitivity as u8];
        c.extend_from_slice(scope.as_bytes());
        c.extend_from_slice(raw);
        let sum = sha(&c);
        c.extend_from_slice(&detail_sum);
        let fingerprint = sha(&c);
        let id = uuid::Uuid::new_v4().as_bytes().to_vec();
        let created = now();
        let observed = optional_time(args, "observed_at")?.unwrap_or_else(|| created.clone());
        let valid_from = optional_time(args, "valid_from")?.unwrap_or_else(|| observed.clone());
        let valid_to = optional_time(args, "valid_to")?;
        ensure(
            valid_to.as_ref().is_none_or(|v| v > &valid_from),
            "invalid validity interval",
        )?;
        let mut expires = optional_time(args, "expires")?;
        if expires.is_none() && [2, 4, 5].contains(&kind) {
            expires = Some(after_days(if kind == 4 { 180 } else { 90 }, &created)?);
        }
        let source_hash = text(args, "source_hash", "")?;
        ensure(
            source_hash.is_empty()
                || (source_hash.len() == 64
                    && source_hash
                        .bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))),
            "invalid source hash",
        )?;
        let mut claim = text(args, "claim_id", "")?.to_owned();
        if claim.is_empty() {
            claim = uuid::Uuid::new_v4().simple().to_string();
        }
        identifier(&claim)?;
        let previous = text(args, "supersedes", "")?;
        let previous_bytes = if previous.is_empty() {
            None
        } else {
            Some(identifier(previous)?)
        };
        if !previous.is_empty() {
            let (prior, _) = self.memory(scope, previous, false)?;
            ensure(
                prior.int("status")? == 0
                    && prior
                        .optional("expires_at")?
                        .is_none_or(|s| s > created.as_str()),
                "correction needs current active version",
            )?;
            claim = prior.text("claim_id")?.to_owned();
        }
        let existing=self.rows("SELECT * FROM cortex_memory WHERE scope=? AND content_fingerprint=? ORDER BY status=0 DESC,created_at DESC LIMIT 1",vec![Sql::Text(scope.into()),Sql::Blob(fingerprint.clone())])?;
        if previous.is_empty()
            && let Some(row) = existing.first()
        {
            self.verify_memory(row)?;
            self.detail(row)?;
            ensure(
                row.int("status")? == 0
                    && row
                        .optional("expires_at")?
                        .is_none_or(|s| s > created.as_str()),
                "duplicate inactive",
            )?;
            ensure(
                row.int("importance")? == importance
                    && row.int("confidence")? == confidence
                    && (row.int("pinned")? != 0) == boolean(args, "pinned", false)?,
                "duplicate metadata differs",
            )?;
            if args.get("expires").is_some_and(|v| !v.is_null()) {
                ensure(
                    row.optional("expires_at")? == expires.as_deref(),
                    "duplicate retention differs",
                )?;
            }
            for key in [
                "observed_at",
                "valid_from",
                "valid_to",
                "source_hash",
                "confidence_reason",
                "claim_id",
            ] {
                if let Some(Value::String(v)) = args.get(key)
                    && !v.is_empty()
                {
                    let wanted = if ["observed_at", "valid_from", "valid_to"].contains(&key) {
                        timestamp(v)?
                    } else {
                        v.clone()
                    };
                    ensure(
                        row.optional(key)? == Some(wanted.as_str()),
                        "duplicate provenance differs",
                    )?;
                }
            }
            let stage = text(args, "stage_id", "")?;
            if !stage.is_empty() {
                let stage = identifier(stage)?;
                match row.0.get("stage_id") {
                    Some(Sql::Blob(old)) => ensure(old == &stage, "duplicate stage differs")?,
                    Some(Sql::Null) => {
                        self.conn.execute(
                            "UPDATE cortex_memory SET stage_id=? WHERE memory_id=?",
                            params![stage, row.bytes("memory_id")?],
                        )?;
                        self.seal("cortex_memory", "memory_id", row.bytes("memory_id")?)?;
                    }
                    _ => return Err("invalid stage".into()),
                };
                self.consolidate(scope, &stage)?;
            }
            return Ok(
                json!({"memory_id":hex::encode(row.bytes("memory_id")?),"deduplicated":true,"status":"active","verified":true}),
            );
        }
        let n:i64=self.conn.query_row("SELECT count(*) FROM cortex_memory WHERE scope=? AND claim_id=? AND status=0 AND memory_id!=?",params![scope,claim,previous_bytes.clone().unwrap_or_default()],|r|r.get(0))?;
        ensure(n == 0, "claim already active")?;
        if let Some(prior) = &previous_bytes {
            self.conn.execute(
                "UPDATE cortex_memory SET status=1,updated_at=? WHERE memory_id=?",
                params![created, prior],
            )?;
            self.seal("cortex_memory", "memory_id", prior)?;
        }
        let stage = text(args, "stage_id", "")?;
        let stage = if stage.is_empty() {
            None
        } else {
            Some(identifier(stage)?)
        };
        let mut values = BTreeMap::new();
        for (k, v) in [
            ("scope", scope),
            ("created_at", &created),
            ("updated_at", &created),
            ("observed_at", &observed),
            ("valid_from", &valid_from),
            ("confidence_reason", reason),
            ("claim_id", &claim),
        ] {
            values.insert(k.into(), Sql::Text(v.into()));
        }
        for (k, v) in [
            ("memory_id", id.clone()),
            ("payload_blob", payload.clone()),
            ("checksum_sha256", sum),
            ("content_fingerprint", fingerprint),
            ("detail_sha256", detail_sum.clone()),
        ] {
            values.insert(k.into(), Sql::Blob(v));
        }
        for (k, v) in [
            ("memory_type", kind),
            ("sensitivity", sensitivity),
            ("importance", importance),
            ("confidence", confidence),
            ("pinned", i64::from(boolean(args, "pinned", false)?)),
            ("payload_raw_bytes", raw.len() as i64),
            ("payload_stored_bytes", payload.len() as i64),
        ] {
            values.insert(k.into(), Sql::Integer(v));
        }
        values.insert("expires_at".into(), sql_time(&expires));
        values.insert("valid_to".into(), sql_time(&valid_to));
        values.insert(
            "source_hash".into(),
            if source_hash.is_empty() {
                Sql::Null
            } else {
                Sql::Text(source_hash.into())
            },
        );
        for key in ["supersedes_id", "prior_version_id"] {
            values.insert(
                key.into(),
                previous_bytes.clone().map(Sql::Blob).unwrap_or(Sql::Null),
            );
        }
        values.insert(
            "stage_id".into(),
            stage.clone().map(Sql::Blob).unwrap_or(Sql::Null),
        );
        self.insert("cortex_memory", values)?;
        self.conn.execute(
            "INSERT INTO cortex_detail VALUES(?,?,?)",
            params![id, prepared.detail, detail_sum],
        )?;
        self.seal("cortex_memory", "memory_id", &id)?;
        let pk: i64 = self.conn.query_row(
            "SELECT memory_pk FROM cortex_memory WHERE memory_id=?",
            [&id],
            |r| r.get(0),
        )?;
        let mut statement = self
            .conn
            .prepare_cached("INSERT INTO cortex_term VALUES(?,?)")?;
        for term in &prepared.terms {
            statement.execute(params![pk, term])?;
        }
        if let Some(stage) = stage {
            self.consolidate(scope, &stage)?;
        }
        Ok(
            json!({"memory_id":hex::encode(id),"deduplicated":false,"supersedes_id":if previous.is_empty(){Value::Null}else{json!(previous)}}),
        )
    }
    pub fn consolidate(&self, scope: &str, id: &[u8]) -> Result<()> {
        let row = self.row("hippocampus_stage", "stage_id", id)?;
        row.verify()?;
        ensure(
            row.text("scope")? == scope
                && row.int("status")? == 0
                && row.text("expires_at")? > now().as_str(),
            "stage unavailable",
        )?;
        let raw = codec::decompress(row.bytes("raw_blob")?)?;
        ensure(
            sha(&raw) == row.bytes("checksum_sha256")?,
            "stage integrity failure",
        )?;
        policy::check_text(std::str::from_utf8(&raw)?, policy::MAX_TEXT)?;
        self.conn.execute("UPDATE hippocampus_stage SET status=1,raw_blob=?,raw_bytes=0,stored_bytes=0,checksum_sha256=? WHERE stage_id=?",params![Vec::<u8>::new(),sha(&[]),id])?;
        self.seal("hippocampus_stage", "stage_id", id)
    }
}

#[cfg(test)]
mod query_plan_tests {
    use super::*;
    #[test]
    fn page_cache_is_bounded_and_does_not_change_durability() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("PRAGMA synchronous=FULL;").unwrap();
        configure_page_cache(&conn, 8 * 1024 * 1024).unwrap();
        assert_eq!(
            conn.pragma_query_value(None, "cache_size", |row| row.get::<_, i64>(0))
                .unwrap(),
            -8192
        );
        assert_eq!(
            conn.pragma_query_value(None, "synchronous", |row| row.get::<_, i64>(0))
                .unwrap(),
            2
        );
        assert!(configure_page_cache(&conn, 1024).is_err());
        assert!(configure_page_cache(&conn, 65 * 1024 * 1024).is_err());
    }
    #[test]
    fn active_claim_check_is_indexed_after_initialise_and_upgrade() {
        let db = Database::initialize(Connection::open_in_memory().unwrap()).unwrap();
        for upgrade in [false, true] {
            if upgrade {
                db.conn
                    .execute_batch("DROP INDEX native_memory_active_claim")
                    .unwrap();
                super::super::knowledge::Knowledge::initialize(&db).unwrap();
            }
            let details=db.conn.prepare("EXPLAIN QUERY PLAN SELECT count(*) FROM cortex_memory WHERE scope=? AND claim_id=? AND status=0 AND memory_id!=?").unwrap()
                .query_map(params!["synthetic:plan","claim",vec![0u8;16]],|r|r.get::<_,String>(3)).unwrap()
                .collect::<rusqlite::Result<Vec<_>>>().unwrap().join(" ");
            assert!(details.contains("native_memory_active_claim"), "{details}");
            assert!(!details.contains("SCAN cortex_memory"), "{details}");
        }
    }
}
