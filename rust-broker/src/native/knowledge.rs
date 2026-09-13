use super::{
    Result, after_days, canonical, codec,
    database::{Database, Row, field, identifier},
    ensure, json, now, policy, sha,
};
use crate::Session;
use rusqlite::{params, types::Value as Sql};
use serde_json::{Value, json as j};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::Read,
    path::{Component, Path, PathBuf},
};
use unicode_casefold::UnicodeCaseFold;

pub fn digest(v: &Value) -> Result<String> {
    Ok(hex::encode(sha(canonical(v, false)?.as_bytes())))
}
fn split_lines(text: &str) -> Vec<&str> {
    static SEPARATORS: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    if text.is_empty() {
        return Vec::new();
    }
    let re = SEPARATORS.get_or_init(|| {
        regex::Regex::new(r"\r\n|[\n\r\x0b\x0c\x1c-\x1e\u0085\u2028\u2029]").unwrap()
    });
    let mut lines: Vec<_> = re.split(text).collect();
    if lines.last() == Some(&"") {
        lines.pop();
    }
    lines
}
pub fn checked(v: &Value) -> Result<()> {
    policy::check_exact(canonical(v, false)?.as_bytes(), "application/json")
}
pub fn keys(v: &Value, required: &[&str], optional: &[&str]) -> Result<()> {
    let m = v.as_object().ok_or("object required")?;
    ensure(
        required.iter().all(|k| m.contains_key(*k))
            && m.keys()
                .all(|k| required.contains(&k.as_str()) || optional.contains(&k.as_str())),
        "invalid object fields",
    )
}
pub fn relative(value: &str) -> Result<()> {
    policy::check_text(value, 512)?;
    ensure(
        !value.is_empty()
            && !value.contains(['\\', ':'])
            && !value.starts_with('/')
            && value
                .split('/')
                .all(|p| !p.is_empty() && p != "." && p != ".."),
        "invalid relative source path",
    )
}
pub(super) struct SourceReader<'a> {
    session: &'a Session,
    root: PathBuf,
    directory: cap_std::fs::Dir,
}

impl<'a> SourceReader<'a> {
    pub(super) fn new(session: &'a Session) -> Result<Self> {
        let root = std::fs::canonicalize(&session.source_root)?;
        ensure(root.is_dir(), "invalid source root")?;
        let directory = cap_std::fs::Dir::open_ambient_dir(&root, cap_std::ambient_authority())?;
        #[cfg(test)]
        SOURCE_ROOT_OPENS.with(|count| count.set(count.get() + 1));
        Ok(Self {
            session,
            root,
            directory,
        })
    }

    pub(super) fn read(&self, path: &str) -> Result<Vec<u8>> {
        relative(path)?;
        let ext = Path::new(path)
            .extension()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_lowercase();
        ensure(
            [
                "txt", "md", "json", "py", "js", "ts", "tsx", "jsx", "go", "rs", "java", "c", "h",
                "cpp", "cs", "rb", "ps1", "toml", "yaml", "yml", "sql",
            ]
            .contains(&ext.as_str()),
            "unsupported source format",
        )?;
        let mut target = self.root.clone();
        for part in Path::new(path).components() {
            ensure(
                matches!(part, Component::Normal(_)),
                "invalid source component",
            )?;
            target.push(part);
            let m = std::fs::symlink_metadata(&target)?;
            ensure(!m.file_type().is_symlink(), "source link rejected")?;
            #[cfg(windows)]
            {
                use std::os::windows::fs::MetadataExt;
                ensure(
                    m.file_attributes() & 0x400 == 0,
                    "source reparse point rejected",
                )?;
            }
        }
        let resolved = std::fs::canonicalize(&target)?;
        ensure(
            resolved.starts_with(&self.root) && resolved.is_file(),
            "source outside root",
        )?;
        let mut raw = Vec::new();
        // Resolve the actual open through a directory capability, so replacement
        // links cannot turn the earlier path checks into an out-of-root read.
        let file = self.directory.open(path)?;
        ensure(file.metadata()?.is_file(), "source must be a regular file")?;
        file.take((policy::MAX_TEXT + 1) as u64)
            .read_to_end(&mut raw)?;
        ensure(
            !raw.is_empty() && raw.len() <= policy::MAX_TEXT,
            "source size limit",
        )?;
        if self.session.redact_secrets {
            ensure(
                ["txt", "md"].contains(&ext.as_str()),
                "redaction supports text and Markdown",
            )?;
            let (body, n) = policy::redact(std::str::from_utf8(&raw)?)?;
            raw = format!(
                "[Redacted source view v1; original SHA256: {}; redactions: {}]\n{}",
                hex::encode(sha(&raw)),
                n,
                body
            )
            .into_bytes();
        }
        policy::check_exact(
            &raw,
            if ext == "json" {
                "application/json"
            } else {
                "text/plain"
            },
        )?;
        Ok(raw)
    }
}

#[cfg(test)]
thread_local! {
    static SOURCE_ROOT_OPENS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

pub fn source_read(s: &Session, path: &str) -> Result<Vec<u8>> {
    SourceReader::new(s)?.read(path)
}
pub fn binding(path: &str, raw: &[u8]) -> Value {
    j!({"path":path,"sha256":hex::encode(sha(raw))})
}
fn validate_binding(v: &Value) -> Result<()> {
    keys(v, &["path", "sha256"], &[])?;
    relative(field(v, "path")?)?;
    let hash = field(v, "sha256")?;
    ensure(
        hash.len() == 64
            && hash
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "invalid source digest",
    )
}
fn source_error_state(error: &(dyn std::error::Error + 'static)) -> String {
    if error
        .downcast_ref::<std::io::Error>()
        .is_some_and(|error| error.kind() == std::io::ErrorKind::NotFound)
    {
        "missing"
    } else {
        "unreadable_or_rejected"
    }
    .into()
}

fn inspect_with(reader: &Result<SourceReader<'_>>, v: &Value) -> String {
    let result = match reader {
        Ok(reader) => reader.read(v["path"].as_str().unwrap_or("")),
        Err(error) => return source_error_state(error.as_ref()),
    };
    match result {
        Ok(raw) => if hex::encode(sha(&raw)) == v["sha256"].as_str().unwrap_or("") {
            "fresh"
        } else {
            "changed"
        }
        .into(),
        Err(error) => source_error_state(error.as_ref()),
    }
}

pub fn inspect(session: &Session, v: &Value) -> String {
    inspect_with(&SourceReader::new(session), v)
}

pub struct Knowledge<'a> {
    pub db: &'a Database,
    pub session: &'a Session,
}
fn candidate_sql(terms: usize, limit: usize, ranked: bool) -> String {
    let base = "m.scope=? AND m.status=0 AND (m.expires_at IS NULL OR m.expires_at>?) AND (m.valid_from IS NULL OR m.valid_from<=?) AND (m.valid_to IS NULL OR m.valid_to>?)";
    if terms == 0 {
        return format!(
            "SELECT m.* FROM cortex_memory m WHERE {base} ORDER BY m.memory_id LIMIT {limit}"
        );
    }
    let order = if ranked {
        "count(DISTINCT t.term_hash) DESC,m.importance DESC,m.confidence DESC,m.memory_id"
    } else {
        "m.memory_id"
    };
    // Start with matching terms; a scope-first plan scans unrelated memories on every recall.
    format!(
        "SELECT m.* FROM cortex_term t INDEXED BY cortex_term_hash CROSS JOIN cortex_memory m ON m.memory_pk=t.memory_pk WHERE {base} AND t.term_hash IN ({}) GROUP BY m.memory_pk ORDER BY {order} LIMIT {limit}",
        vec!["?"; terms].join(",")
    )
}
impl<'a> Knowledge<'a> {
    pub fn initialize(db: &Database) -> Result<()> {
        super::chat_lifecycle::initialise(db)?;
        super::retention::initialise(db)?;
        db.conn.execute_batch("CREATE TABLE IF NOT EXISTS knowledge_format(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL);INSERT OR IGNORE INTO knowledge_format VALUES(1,1);
        CREATE TABLE IF NOT EXISTS knowledge_item(id TEXT PRIMARY KEY,scope TEXT NOT NULL,kind TEXT NOT NULL,owner BLOB REFERENCES cortex_memory(memory_id) ON DELETE CASCADE,target BLOB REFERENCES cortex_memory(memory_id) ON DELETE CASCADE,payload TEXT NOT NULL,checksum TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS knowledge_scope_kind ON knowledge_item(scope,kind);
        CREATE INDEX IF NOT EXISTS knowledge_scope_owner_kind ON knowledge_item(scope,owner,kind);
        CREATE INDEX IF NOT EXISTS knowledge_scope_id ON knowledge_item(scope,id);
        CREATE INDEX IF NOT EXISTS knowledge_scope_target_kind ON knowledge_item(scope,target,kind);
        CREATE INDEX IF NOT EXISTS knowledge_graph_owner ON knowledge_item(scope,owner,id) WHERE kind='relation';
        CREATE INDEX IF NOT EXISTS knowledge_graph_target ON knowledge_item(scope,target,id) WHERE kind='relation';
        CREATE INDEX IF NOT EXISTS native_knowledge_owner ON knowledge_item(owner);
        CREATE INDEX IF NOT EXISTS native_knowledge_target ON knowledge_item(target);
        CREATE INDEX IF NOT EXISTS native_memory_scope_id ON cortex_memory(scope,memory_id);
        CREATE INDEX IF NOT EXISTS native_memory_active_claim ON cortex_memory(scope,claim_id,memory_id) WHERE status=0;
        CREATE INDEX IF NOT EXISTS native_memory_supersedes ON cortex_memory(supersedes_id);
        CREATE INDEX IF NOT EXISTS native_verbatim_link ON cortex_verbatim(linked_memory_id);
        CREATE TABLE IF NOT EXISTS session_route(scope TEXT NOT NULL,message_id TEXT NOT NULL,payload TEXT NOT NULL,checksum TEXT NOT NULL,PRIMARY KEY(scope,message_id));")?;
        for op in ["INSERT", "UPDATE", "DELETE"] {
            db.conn.execute_batch(&format!("CREATE TRIGGER IF NOT EXISTS revision_knowledge_{op} AFTER {op} ON knowledge_item BEGIN UPDATE vault_state SET revision=revision+1 WHERE singleton=1; END;"))?;
        }
        Ok(())
    }
    pub fn new(db: &'a Database, session: &'a Session) -> Result<Self> {
        ensure(
            db.conn.query_row(
                "SELECT version FROM knowledge_format WHERE singleton=1",
                [],
                |r| r.get::<_, i64>(0),
            )? == 1,
            "unsupported knowledge format",
        )?;
        policy::check_text(&session.scope, 256)?;
        ensure(!session.scope.trim().is_empty(), "scope required")?;
        Ok(Self { db, session })
    }
    pub fn items(&self, kind: Option<&str>) -> Result<Vec<Value>> {
        let (sql, values) = if let Some(k) = kind {
            (
                "SELECT * FROM knowledge_item WHERE scope=? AND kind=? ORDER BY id LIMIT 2001",
                vec![Sql::Text(self.session.scope.clone()), Sql::Text(k.into())],
            )
        } else {
            (
                "SELECT * FROM knowledge_item WHERE scope=? ORDER BY id LIMIT 2001",
                vec![Sql::Text(self.session.scope.clone())],
            )
        };
        let rows = self.db.rows(sql, values)?;
        ensure(rows.len() <= 2000, "knowledge scope limit")?;
        self.decode_items(rows)
    }
    fn decode_items(&self, rows: Vec<Row>) -> Result<Vec<Value>> {
        let mut result = Vec::new();
        let mut sources = BTreeSet::new();
        let mut alias = 0;
        for row in rows {
            let link = |k: &str| -> Result<Value> {
                Ok(match row.0.get(k) {
                    Some(Sql::Null) => Value::Null,
                    Some(Sql::Blob(b)) => j!(hex::encode(b)),
                    _ => return Err("invalid knowledge link".into()),
                })
            };
            let item = j!({"id":row.text("id")?,"scope":row.text("scope")?,"kind":row.text("kind")?,"owner":link("owner")?,"target":link("target")?,"payload":json(row.text("payload")?)?});
            ensure(
                digest(&item)? == row.text("checksum")?,
                "knowledge integrity failure",
            )?;
            self.validate(&item)?;
            if item["kind"] == "aliases" {
                alias += 1;
            }
            if item["kind"] == "source" {
                ensure(
                    sources.insert((
                        field(&item, "owner")?.to_owned(),
                        field(&item["payload"], "path")?.to_owned(),
                    )),
                    "duplicate source binding",
                )?;
            }
            result.push(item);
        }
        ensure(alias <= 1, "multiple alias sets")?;
        Ok(result)
    }
    pub fn page(&self, after: &str, limit: usize) -> Result<Vec<Value>> {
        ensure((1..=200).contains(&limit), "page size out of range")?;
        if !after.is_empty() {
            identifier(after)?;
        }
        self.decode_items(self.db.rows(
            "SELECT * FROM knowledge_item WHERE scope=? AND id>? ORDER BY id LIMIT ?",
            vec![
                Sql::Text(self.session.scope.clone()),
                Sql::Text(after.into()),
                Sql::Integer(limit as i64),
            ],
        )?)
    }
    pub fn verify_items(&self) -> Result<()> {
        let mut after = String::new();
        loop {
            let page = self.page(&after, 200)?;
            let Some(last) = page.last() else { break };
            after = field(last, "id")?.into();
        }
        let aliases: i64 = self.db.conn.query_row(
            "SELECT count(*) FROM knowledge_item WHERE scope=? AND kind='aliases'",
            [&self.session.scope],
            |r| r.get(0),
        )?;
        ensure(aliases <= 1, "multiple alias sets")?;
        let duplicates: bool = self.db.conn.query_row("SELECT EXISTS(SELECT 1 FROM knowledge_item WHERE scope=? AND kind='source' GROUP BY owner,json_extract(payload,'$.path') HAVING count(*)>1)",[&self.session.scope],|r|r.get(0))?;
        ensure(!duplicates, "duplicate source binding")
    }
    pub fn related_items(&self, id: &str) -> Result<Vec<Value>> {
        let id = identifier(id)?;
        let rows = self.db.rows("SELECT * FROM knowledge_item WHERE scope=? AND kind='relation' AND (owner=? OR target=?) ORDER BY id LIMIT 2001",
            vec![Sql::Text(self.session.scope.clone()),Sql::Blob(id.clone()),Sql::Blob(id)])?;
        ensure(rows.len() <= 2000, "per-record relation limit")?;
        self.decode_items(rows)
    }
    pub fn validate(&self, item: &Value) -> Result<()> {
        checked(item)?;
        keys(
            item,
            &["id", "scope", "kind", "owner", "target", "payload"],
            &[],
        )?;
        identifier(field(item, "id")?)?;
        ensure(
            item["scope"] == self.session.scope,
            "knowledge scope mismatch",
        )?;
        for k in ["owner", "target"] {
            if !item[k].is_null() {
                self.db
                    .memory(&self.session.scope, field(item, k)?, false)?;
            }
        }
        let p = &item["payload"];
        ensure(p.is_object(), "payload required")?;
        match field(item, "kind")? {
            "source" => {
                ensure(
                    item["owner"].is_string() && item["target"].is_null(),
                    "invalid source owner",
                )?;
                validate_binding(p)?;
            }
            "proposal" => {
                ensure(
                    item["owner"].is_null() && item["target"].is_null(),
                    "invalid proposal owner",
                )?;
                keys(
                    p,
                    &[
                        "binding",
                        "subject",
                        "summary",
                        "type",
                        "line_start",
                        "line_end",
                        "reviewed",
                        "created_at",
                        "expires_at",
                    ],
                    &[],
                )?;
                validate_binding(&p["binding"])?;
                ensure(
                    p["reviewed"] == false
                        && ["semantic", "episodic", "procedural"].contains(&field(p, "type")?),
                    "invalid proposal state",
                )?;
                policy::check_text(field(p, "subject")?, 512)?;
                policy::check_text(field(p, "summary")?, 8192)?;
                ensure(
                    !field(p, "subject")?.trim().is_empty()
                        && !field(p, "summary")?.trim().is_empty(),
                    "empty proposal",
                )?;
                let start = p["line_start"].as_u64().ok_or("invalid line")?;
                let end = p["line_end"].as_u64().ok_or("invalid line")?;
                ensure(
                    start >= 1 && end >= start && end <= 50000,
                    "invalid line range",
                )?;
                ensure(
                    super::timestamp(field(p, "created_at")?)? == field(p, "created_at")?
                        && after_days(1, field(p, "created_at")?)? == field(p, "expires_at")?,
                    "invalid proposal retention",
                )?;
            }
            "aliases" => {
                ensure(
                    item["owner"].is_null() && item["target"].is_null(),
                    "invalid alias owner",
                )?;
                keys(p, &["groups"], &[])?;
                alias_groups(&p["groups"])?;
            }
            "relation" => {
                keys(p, &["relation", "evidence"], &[])?;
                ensure(
                    item["owner"].is_string()
                        && item["target"].is_string()
                        && item["owner"] != item["target"],
                    "invalid relation endpoints",
                )?;
                ensure(
                    ["supported_by", "applies_to", "contradicts", "depends_on"]
                        .contains(&field(p, "relation")?)
                        && !field(p, "evidence")?.trim().is_empty(),
                    "invalid relation",
                )?;
            }
            _ => return Err("unsupported knowledge kind".into()),
        }
        Ok(())
    }
    pub fn put(
        &self,
        kind: &str,
        payload: Value,
        owner: Option<&str>,
        target: Option<&str>,
    ) -> Result<Value> {
        let item = j!({"id":uuid::Uuid::new_v4().simple().to_string(),"scope":self.session.scope,"kind":kind,"owner":owner,"target":target,"payload":payload});
        self.validate(&item)?;
        if kind == "proposal" {
            let n: i64 = self.db.conn.query_row(
                "SELECT count(*) FROM knowledge_item WHERE scope=? AND kind='proposal'",
                [&self.session.scope],
                |r| r.get(0),
            )?;
            ensure(
                n < 2000,
                "pending proposal limit; review or expire existing proposals",
            )?;
        }
        if kind == "source" {
            let n: i64 = self.db.conn.query_row(
                "SELECT count(*) FROM knowledge_item WHERE scope=? AND owner=? AND kind='source'",
                params![self.session.scope, owner.map(identifier).transpose()?],
                |r| r.get(0),
            )?;
            ensure(n < 2000, "per-record source limit")?;
        }
        if kind == "relation" {
            for id in [owner, target].into_iter().flatten() {
                ensure(
                    self.related_items(id)?.len() < 2000,
                    "per-record relation limit",
                )?;
            }
        }
        self.db.conn.execute(
            "INSERT INTO knowledge_item VALUES(?,?,?,?,?,?,?)",
            params![
                field(&item, "id")?,
                self.session.scope,
                kind,
                owner.map(identifier).transpose()?,
                target.map(identifier).transpose()?,
                canonical(&item["payload"], false)?,
                digest(&item)?
            ],
        )?;
        Ok(item)
    }
    pub fn expire(&self) -> Result<Value> {
        let mut n = 0;
        for item in self.items(Some("proposal"))? {
            if field(&item["payload"], "expires_at")? <= now().as_str() {
                self.db.conn.execute(
                    "DELETE FROM knowledge_item WHERE id=? AND scope=?",
                    params![field(&item, "id")?, self.session.scope],
                )?;
                n += 1;
            }
        }
        Ok(j!({"expired_proposals_disposed":n}))
    }
    pub fn propose(&self, path: &str) -> Result<Value> {
        super::retention::check_source(self, path)?;
        let raw = source_read(self.session, path)?;
        let source = std::str::from_utf8(&raw)?;
        let lines = split_lines(source);
        let re = regex::Regex::new(
            r"^(Decision|Fact|Procedure|Episode|Requirement|Constraint|Unfinished):\s+(.+)$",
        )?;
        let starts: Vec<_> = lines
            .iter()
            .enumerate()
            .filter_map(|(n, l)| {
                re.captures(l).map(|c| {
                    (
                        n,
                        c[1].to_owned(),
                        c[2].chars().take(180).collect::<String>(),
                    )
                })
            })
            .collect();
        ensure(starts.len() <= 64, "too many proposal spans")?;
        let mut spans = Vec::new();
        for (i, (start, kind, subject)) in starts.iter().enumerate() {
            let mut end = starts.get(i + 1).map(|s| s.0).unwrap_or(lines.len());
            if let Some(stop) = ((*start + 1)..end).find(|n| lines[*n] == "End memory.") {
                end = stop;
            }
            while end > start + 1 && lines[end - 1].trim().is_empty() {
                end -= 1;
            }
            spans.push(j!({"line_start":start+1,"line_end":end,"subject":subject,"type":match kind.as_str(){"Procedure"=>"procedural","Episode"=>"episodic",_=>"semantic"}}));
        }
        self.propose_spans(path, &Value::Array(spans), &hex::encode(sha(&raw)))
    }
    pub fn propose_spans(&self, path: &str, spans: &Value, expected: &str) -> Result<Value> {
        super::retention::check_source(self, path)?;
        let raw = source_read(self.session, path)?;
        ensure(
            hex::encode(sha(&raw)) == expected,
            "source changed before extraction",
        )?;
        let lines = split_lines(std::str::from_utf8(&raw)?);
        let spans = spans.as_array().ok_or("invalid spans")?;
        ensure(spans.len() <= 64, "too many spans")?;
        let created = now();
        let mut prepared = Vec::new();
        for span in spans {
            checked(span)?;
            keys(span, &["line_start", "line_end", "subject", "type"], &[])?;
            let start = span["line_start"].as_u64().ok_or("invalid line")? as usize;
            let end = span["line_end"].as_u64().ok_or("invalid line")? as usize;
            ensure(
                start >= 1 && end >= start && end <= lines.len(),
                "invalid evidence range",
            )?;
            let mut p = span.clone();
            p["summary"] = j!(lines[start - 1..end].join("\n"));
            p["binding"] = binding(path, &raw);
            p["reviewed"] = j!(false);
            p["created_at"] = j!(created);
            p["expires_at"] = j!(after_days(1, &created)?);
            prepared.push(p);
        }
        self.expire()?;
        let key = |p: &Value| -> Result<String> {
            let mut p = p.clone();
            p.as_object_mut()
                .ok_or("invalid proposal")?
                .remove("created_at");
            p.as_object_mut().unwrap().remove("expires_at");
            digest(&p)
        };
        let mut existing = BTreeMap::new();
        for i in self.items(Some("proposal"))? {
            existing.insert(key(&i["payload"])?, i);
        }
        let mut result = Vec::new();
        for p in prepared {
            let k = key(&p)?;
            if !existing.contains_key(&k) {
                existing.insert(k.clone(), self.put("proposal", p, None, None)?);
            }
            let item = &existing[&k];
            let mut proposal = item["payload"].clone();
            proposal["id"] = item["id"].clone();
            proposal["review_digest"] = j!(digest(item)?);
            result.push(proposal);
        }
        Ok(
            j!({"proposals":result,"extraction":"verbatim_evidence_spans","automatic_truth_verification":false}),
        )
    }
    pub fn review(&self, id: &str) -> Result<Value> {
        identifier(id)?;
        let item = self
            .decode_items(self.db.rows(
                "SELECT * FROM knowledge_item WHERE scope=? AND kind='proposal' AND id=?",
                vec![Sql::Text(self.session.scope.clone()), Sql::Text(id.into())],
            )?)?
            .into_iter()
            .find(|i| i["id"] == id)
            .ok_or("proposal not found")?;
        Ok(
            j!({"review_digest":digest(&item)?,"source_state":inspect(self.session,&item["payload"]["binding"]),"expired":field(&item["payload"],"expires_at")?<=now().as_str(),"item":item}),
        )
    }
    pub fn bind(&self, id: &str, path: &str, expected: &str) -> Result<Value> {
        let raw = source_read(self.session, path)?;
        let b = binding(path, &raw);
        ensure(
            field(&b, "sha256")? == expected,
            "source changed before binding",
        )?;
        let (row, _) = self.db.memory(&self.session.scope, id, true)?;
        ensure(
            row.optional("source_hash")?.is_none_or(|v| v == expected),
            "source digest disagreement",
        )?;
        super::chat_lifecycle::bind_source(self, id, path)?;
        if let Some(item) = self
            .source_items(&[id.to_owned()])?
            .into_iter()
            .find(|i| i["owner"] == id && i["payload"]["path"] == path)
        {
            ensure(item["payload"] == b, "rebinding needs corrected memory")?;
            super::vectors::queue_bound(self, id)?;
            return Ok(item);
        }
        let item = self.put("source", b, Some(id), None)?;
        super::vectors::queue_bound(self, id)?;
        Ok(item)
    }
    pub fn accept(&self, id: &str, review: &str, previous: Option<&str>) -> Result<Value> {
        let r = self.review(id)?;
        ensure(
            r["review_digest"] == review && r["source_state"] == "fresh" && r["expired"] == false,
            "proposal review stale",
        )?;
        let p = &r["item"]["payload"];
        if let Some(old) = previous {
            self.db.memory(&self.session.scope, old, true)?;
        } else {
            let mut after = Vec::<u8>::new();
            let clock = now();
            loop {
                let rows = self.db.rows("SELECT * FROM cortex_memory WHERE scope=? AND memory_id>? AND status=0 AND (expires_at IS NULL OR expires_at>?) AND (valid_from IS NULL OR valid_from<=?) AND (valid_to IS NULL OR valid_to>?) ORDER BY memory_id LIMIT 128",
                    vec![Sql::Text(self.session.scope.clone()),Sql::Blob(after.clone()),Sql::Text(clock.clone()),Sql::Text(clock.clone()),Sql::Text(clock.clone())])?;
                if rows.is_empty() {
                    break;
                }
                for row in rows {
                    after = row.bytes("memory_id")?.to_vec();
                    let payload = self.db.verify_memory(&row)?;
                    ensure(
                        payload[0].case_fold().collect::<String>()
                            != field(p, "subject")?.case_fold().collect::<String>(),
                        "existing subject requires correction",
                    )?;
                }
            }
        }
        let result=self.db.remember(&self.session.scope,&j!({"type":p["type"],"subject":p["subject"],"summary":p["summary"],"source":p["binding"]["path"],"source_hash":p["binding"]["sha256"],"confidence":0.5,
            "confidence_reason":"Reviewed source excerpt; factual truth not independently verified","supersedes":previous}))?;
        self.bind(
            field(&result, "memory_id")?,
            field(&p["binding"], "path")?,
            field(&p["binding"], "sha256")?,
        )?;
        self.db.conn.execute(
            "DELETE FROM knowledge_item WHERE id=? AND scope=?",
            params![id, self.session.scope],
        )?;
        Ok(result)
    }
    pub fn reject(&self, id: &str, review: &str) -> Result<Value> {
        ensure(
            self.review(id)?["review_digest"] == review,
            "proposal review stale",
        )?;
        self.db.conn.execute(
            "DELETE FROM knowledge_item WHERE id=? AND scope=?",
            params![id, self.session.scope],
        )?;
        Ok(j!({"discarded":true}))
    }
    pub fn source_items(&self, ids: &[String]) -> Result<Vec<Value>> {
        let mut items = Vec::new();
        let mut seen = BTreeSet::new();
        let mut statement=self.db.conn.prepare("SELECT id,payload,checksum FROM knowledge_item WHERE scope=? AND owner=? AND kind='source' LIMIT 2001")?;
        for id in ids {
            let rows = statement.query_map(params![self.session.scope, identifier(id)?], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                ))
            })?;
            for row in rows {
                let (key, raw, sum) = row?;
                let item = j!({"id":key,"scope":self.session.scope,"kind":"source","owner":id,"target":null,"payload":json(&raw)?});
                ensure(digest(&item)? == sum, "source integrity failure")?;
                self.validate(&item)?;
                ensure(
                    seen.insert((id.to_owned(), field(&item["payload"], "path")?.to_owned())),
                    "duplicate source binding",
                )?;
                items.push(item);
                ensure(items.len() <= 2000, "source selection limit")?;
            }
        }
        Ok(items)
    }
    pub fn freshness(&self, id: &str) -> Result<Value> {
        self.db.memory(&self.session.scope, id, true)?;
        let reader = SourceReader::new(self.session);
        self.freshness_with(
            id,
            &self.source_items(&[id.to_owned()])?,
            &reader,
            &mut BTreeMap::new(),
        )
    }
    pub(super) fn freshness_with(
        &self,
        id: &str,
        items: &[Value],
        reader: &Result<SourceReader<'_>>,
        cache: &mut BTreeMap<String, String>,
    ) -> Result<Value> {
        let mut sources = Vec::new();
        for item in items.iter().filter(|i| i["owner"] == id) {
            let b = &item["payload"];
            let path = field(b, "path")?;
            if !super::chat_lifecycle::source_allowed(self, id, path)? {
                continue;
            }
            let state = if let Some(hash) = cache.get(path) {
                if hash.len() == 64 {
                    if hash == field(b, "sha256")? {
                        "fresh"
                    } else {
                        "changed"
                    }
                    .to_owned()
                } else {
                    hash.clone()
                }
            } else {
                let actual = match reader {
                    Ok(reader) => match reader.read(path) {
                        Ok(raw) => hex::encode(sha(&raw)),
                        Err(error) => source_error_state(error.as_ref()),
                    },
                    Err(error) => source_error_state(error.as_ref()),
                };
                let state = if actual.len() == 64 {
                    if actual == field(b, "sha256")? {
                        "fresh"
                    } else {
                        "changed"
                    }
                    .into()
                } else {
                    actual.clone()
                };
                cache.insert(path.into(), actual);
                state
            };
            sources.push(j!({"path":path,"state":state}));
        }
        let state = if sources.is_empty() {
            "unverified"
        } else if sources.iter().all(|s| s["state"] == "fresh") {
            "fresh"
        } else {
            "stale"
        };
        Ok(j!({"state":state,"sources":sources}))
    }
    pub fn candidates(&self, terms: &BTreeSet<String>) -> Result<Vec<Row>> {
        let rows = self.select_candidates(terms, 2001, false)?;
        ensure(rows.len() <= 2000, "native candidate limit exceeded")?;
        Ok(rows)
    }
    pub(super) fn lexical_ids(&self, query: &str, limit: usize) -> Result<(Vec<String>, bool)> {
        policy::check_text(query, 4096)?;
        let terms = codec::tokenize(query);
        ensure(terms.len() <= 64 && limit <= 256, "candidate query bound")?;
        if terms.is_empty() {
            return Ok((Vec::new(), false));
        }
        let rows = self.select_candidates(&terms, limit + 1, true)?;
        let capped = rows.len() > limit;
        let ids = rows
            .iter()
            .take(limit)
            .map(|row| row.bytes("memory_id").map(hex::encode))
            .collect::<Result<Vec<_>>>()?;
        Ok((ids, capped))
    }
    fn select_candidates(
        &self,
        terms: &BTreeSet<String>,
        limit: usize,
        ranked: bool,
    ) -> Result<Vec<Row>> {
        let now = now();
        let mut p = vec![
            Sql::Text(self.session.scope.clone()),
            Sql::Text(now.clone()),
            Sql::Text(now.clone()),
            Sql::Text(now),
        ];
        for t in terms {
            p.push(Sql::Blob(codec::term_hash(t)));
        }
        let sql = candidate_sql(terms.len(), limit, ranked);
        let rows = self.db.rows(&sql, p)?;
        Ok(rows)
    }
    pub fn recall(
        &self,
        query: &str,
        mode: &str,
        limit: usize,
        max_tokens: usize,
    ) -> Result<Value> {
        policy::check_text(query, 4096)?;
        ensure(
            (1..=100).contains(&limit)
                && (64..=8000).contains(&max_tokens)
                && ["lexical", "aliases", "hybrid"].contains(&mode),
            "invalid recall settings",
        )?;
        let terms = codec::tokenize(query);
        ensure(terms.len() <= 64, "query too large")?;
        let mut expanded = terms.clone();
        let mut groups = Vec::<Vec<String>>::new();
        if mode != "lexical" {
            for item in self.items(Some("aliases"))? {
                for group in item["payload"]["groups"]
                    .as_array()
                    .ok_or("invalid groups")?
                {
                    let group: Vec<String> = group
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|v| v.as_str().unwrap().into())
                        .collect();
                    if group.iter().any(|w| terms.contains(w)) {
                        expanded.extend(group.clone());
                    }
                    groups.push(group);
                }
            }
        }
        let mut rows = if terms.is_empty() {
            vec![]
        } else {
            self.select_candidates(&expanded, 257, true)?
        };
        let capped = rows.len() > 256;
        rows.truncate(256);
        let ids = rows
            .iter()
            .map(|r| r.bytes("memory_id").map(hex::encode))
            .collect::<Result<Vec<_>>>()?;
        let sources = self.source_items(&ids)?;
        let reader = SourceReader::new(self.session);
        let mut cache = BTreeMap::new();
        let mut scored = Vec::new();
        let (mut stale, mut unverified) = (0, 0);
        for row in rows {
            let payload = self.db.verify_memory(&row)?;
            let detail = self.db.detail(&row)?;
            let words = codec::tokenize(&format!(
                "{} {} {} {}",
                payload[0], payload[1], payload[3], detail
            ));
            let hits = words.intersection(&expanded).count();
            if hits == 0 {
                continue;
            }
            let id = hex::encode(row.bytes("memory_id")?);
            let fresh = self.freshness_with(&id, &sources, &reader, &mut cache)?;
            if fresh["state"] == "stale" {
                stale += 1;
                continue;
            }
            if fresh["state"] == "unverified" {
                unverified += 1;
                continue;
            }
            let importance = round(row.int("importance")? as f64 / 255.0, 3);
            let confidence = round(row.int("confidence")? as f64 / 255.0, 3);
            let mut score = 0.60 * hits as f64 / expanded.len().max(1) as f64
                + 0.25 * importance
                + 0.15 * confidence;
            if mode == "hybrid" {
                let (mut cw, mut cq) = (words.clone(), terms.clone());
                for (n, g) in groups.iter().enumerate() {
                    if g.iter().any(|w| words.contains(w)) {
                        for w in g {
                            cw.remove(w);
                        }
                        cw.insert(format!("concept:{n}"));
                    }
                    if g.iter().any(|w| terms.contains(w)) {
                        for w in g {
                            cq.remove(w);
                        }
                        cq.insert(format!("concept:{n}"));
                    }
                }
                score = 0.5 * score
                    + 0.5 * cw.intersection(&cq).count() as f64
                        / ((cw.len() * cq.len()).max(1) as f64).sqrt();
            }
            scored.push(j!({"id":id,"subject":payload[0],"summary":payload[1],"source":payload[4],"source_hash":row.value("source_hash")?,"confidence":confidence,"confidence_reason":row.text("confidence_reason")?,
                "observed_at":row.text("observed_at")?,"valid_from":if row.optional("valid_from")?==Some(row.text("observed_at")?){Value::Null}else{row.value("valid_from")?},"valid_to":row.value("valid_to")?,"freshness":fresh,"score":round(score,6)}));
        }
        scored.sort_by(|a, b| {
            b["score"]
                .as_f64()
                .unwrap()
                .total_cmp(&a["score"].as_f64().unwrap())
                .then_with(|| a["id"].as_str().cmp(&b["id"].as_str()))
        });
        let revision: i64 = self.db.conn.query_row(
            "SELECT revision FROM vault_state WHERE singleton=1",
            [],
            |r| r.get(0),
        )?;
        let mut result = j!({"scope":self.session.scope,"mode":mode,"data_only":true,"memories":[],"omitted":scored.len(),"excluded":{"stale":stale,"unverified":unverified},"revision":revision});
        if capped {
            result["candidates_capped"] = j!(true);
            result["candidate_limit"] = j!(256);
            result["unexamined_matches"] = Value::Null;
        }
        let mut selected = Vec::new();
        for item in scored.iter().take(limit) {
            let mut trial = selected.clone();
            trial.push(item.clone());
            let mut candidate = result.clone();
            candidate["memories"] = j!(trial);
            candidate["omitted"] = j!(scored.len() - trial.len());
            if tokens(&canonical(&candidate, false)?) <= max_tokens {
                selected = trial;
                result = candidate;
            }
        }
        ensure(
            tokens(&canonical(&result, false)?) <= max_tokens,
            "recall receipt exceeds budget",
        )?;
        Ok(result)
    }
    pub fn recall_compact(&self, query: &str, mode: &str, max_tokens: usize) -> Result<Value> {
        let recall = self.recall(query, mode, 8, 8000)?;
        self.compact_recall(&recall, mode, max_tokens)
    }
    pub fn compact_recall(&self, recall: &Value, mode: &str, max_tokens: usize) -> Result<Value> {
        let mut rows = Vec::new();
        for mut row in recall["memories"].as_array().unwrap().clone() {
            let (id, _) = self
                .db
                .memory(&self.session.scope, field(&row, "id")?, true)?;
            row["type"] = j!(memory_type(id.int("memory_type")?)?);
            rows.push(row);
        }
        let mut included = Vec::new();
        let mut body = j!({"source_checked":true,"requires_fresh":true,"excluded":recall["excluded"],"mode":mode,"retrieval_omitted":recall["omitted"],"packet":""});
        for key in [
            "vector_state",
            "pending_index_updates",
            "index_cache_hit",
            "relevance_filtered",
        ] {
            if let Some(value) = recall.get(key) {
                body[key] = value.clone();
            }
        }
        if recall["candidates_capped"] == true {
            body["candidates_capped"] = j!(true);
            body["unexamined_matches"] = Value::Null;
        }
        for row in &rows {
            let mut trial = included.clone();
            trial.push(row.clone());
            let packet = render(
                &self.session.scope,
                &trial,
                rows.len() - trial.len(),
                recall["candidates_capped"] == true || recall["omitted"].as_u64().unwrap_or(0) > 0,
            )?;
            let mut candidate = body.clone();
            candidate["packet"] = j!(packet);
            if tokens(&canonical(&candidate, false)?) <= max_tokens {
                included = trial;
            }
        }
        body["packet"] = j!(render(
            &self.session.scope,
            &included,
            rows.len() - included.len(),
            recall["candidates_capped"] == true || recall["omitted"].as_u64().unwrap_or(0) > 0
        )?);
        ensure(
            tokens(&canonical(&body, false)?) <= max_tokens,
            "packet budget too small",
        )?;
        Ok(body)
    }
}
pub fn tokens(s: &str) -> usize {
    tiktoken_rs::o200k_base_singleton().encode_ordinary(s).len()
}
fn round(n: f64, d: i32) -> f64 {
    let p = 10f64.powi(d);
    (n * p).round_ties_even() / p
}
pub(super) fn memory_type(n: i64) -> Result<&'static str> {
    match n {
        1 => Ok("semantic"),
        2 => Ok("episodic"),
        3 => Ok("procedural"),
        4 => Ok("priming_conditioning"),
        5 => Ok("classical_conditioning"),
        _ => Err("unknown memory type".into()),
    }
}
pub fn alias_groups(v: &Value) -> Result<()> {
    let groups = v.as_array().ok_or("invalid aliases")?;
    ensure(groups.len() <= 64, "too many aliases")?;
    for g in groups {
        let group = g.as_array().ok_or("invalid alias group")?;
        ensure((2..=8).contains(&group.len()), "invalid alias group size")?;
        let mut seen = BTreeSet::new();
        for w in group {
            let w = w.as_str().ok_or("invalid alias")?;
            ensure(
                (2..=32).contains(&w.len())
                    && w.bytes().all(|b| b.is_ascii_lowercase())
                    && seen.insert(w),
                "invalid alias",
            )?;
        }
    }
    Ok(())
}
fn render(scope: &str, rows: &[Value], omitted: usize, capped: bool) -> Result<String> {
    let mut shared = serde_json::Map::new();
    shared.insert("scope".into(), j!(scope));
    for k in ["type", "source", "confidence", "observed_at"] {
        if let Some(first) = rows.first()
            && rows.iter().all(|r| r[k] == first[k])
        {
            shared.insert(k.into(), first[k].clone());
        }
    }
    let mut lines = vec![format!(
        "Memory data; {}",
        canonical(&Value::Object(shared.clone()), false)?
    )];
    for row in rows {
        let mut out = serde_json::Map::new();
        for k in ["type", "source", "confidence", "observed_at"] {
            if !shared.contains_key(k) {
                out.insert(k.into(), row[k].clone());
            }
        }
        for k in ["subject", "summary", "id"] {
            out.insert(k.into(), row[k].clone());
        }
        for k in [
            "valid_from",
            "valid_to",
            "source_hash",
            "confidence_reason",
            "detail",
        ] {
            if !row[k].is_null() && row[k] != j!("") {
                out.insert(k.into(), row[k].clone());
            }
        }
        lines.push(canonical(&Value::Object(out), false)?);
    }
    lines.push(format!("omitted={omitted}; candidates_capped={capped}"));
    Ok(lines.join("\n"))
}

#[cfg(test)]
mod candidate_tests {
    use super::*;

    #[test]
    fn request_reader_reuses_one_root_capability_without_caching_source_bytes() {
        let root =
            std::env::temp_dir().join(format!("memorycore-ai-reader-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&root).unwrap();
        std::fs::write(root.join("first.md"), "first version").unwrap();
        std::fs::write(root.join("second.md"), "second source").unwrap();
        for index in 0..64 {
            std::fs::write(
                root.join(format!("source-{index}.md")),
                format!("source {index}"),
            )
            .unwrap();
        }
        let session: crate::Session = serde_json::from_value(j!({
            "id":"reader","scope":"synthetic:reader","source_root":root,
        }))
        .unwrap();

        SOURCE_ROOT_OPENS.with(|count| count.set(0));
        let reader = SourceReader::new(&session).unwrap();
        assert_eq!(reader.read("first.md").unwrap(), b"first version");
        assert_eq!(reader.read("second.md").unwrap(), b"second source");
        assert!(reader.read("../outside.md").is_err());
        assert_eq!(SOURCE_ROOT_OPENS.with(std::cell::Cell::get), 1);

        std::fs::write(root.join("first.md"), "changed immediately").unwrap();
        assert_eq!(reader.read("first.md").unwrap(), b"changed immediately");
        drop(reader);
        let next_request = SourceReader::new(&session).unwrap();
        assert_eq!(
            next_request.read("first.md").unwrap(),
            b"changed immediately"
        );
        assert_eq!(SOURCE_ROOT_OPENS.with(std::cell::Cell::get), 2);
        drop(next_request);

        SOURCE_ROOT_OPENS.with(|count| count.set(0));
        let repeated_started = std::time::Instant::now();
        for index in 0..64 {
            source_read(&session, &format!("source-{index}.md")).unwrap();
        }
        let repeated = repeated_started.elapsed();
        assert_eq!(SOURCE_ROOT_OPENS.with(std::cell::Cell::get), 64);
        SOURCE_ROOT_OPENS.with(|count| count.set(0));
        let reused_started = std::time::Instant::now();
        let benchmark_reader = SourceReader::new(&session).unwrap();
        for index in 0..64 {
            benchmark_reader
                .read(&format!("source-{index}.md"))
                .unwrap();
        }
        let reused = reused_started.elapsed();
        assert_eq!(SOURCE_ROOT_OPENS.with(std::cell::Cell::get), 1);
        eprintln!("64-source validation: repeated_root={repeated:?}, reused_root={reused:?}");
        drop(benchmark_reader);

        std::fs::remove_file(root.join("first.md")).unwrap();
        std::fs::remove_file(root.join("second.md")).unwrap();
        for index in 0..64 {
            std::fs::remove_file(root.join(format!("source-{index}.md"))).unwrap();
        }
        std::fs::remove_dir(root).unwrap();
    }

    #[test]
    fn request_reader_rejects_source_links() {
        let base =
            std::env::temp_dir().join(format!("memorycore-ai-link-{}", uuid::Uuid::new_v4()));
        let root = base.join("root");
        std::fs::create_dir_all(&root).unwrap();
        let outside = base.join("outside.md");
        let link = root.join("linked.md");
        std::fs::write(&outside, "outside source").unwrap();
        #[cfg(windows)]
        if let Err(error) = std::os::windows::fs::symlink_file(&outside, &link) {
            if error.kind() == std::io::ErrorKind::PermissionDenied
                || error.raw_os_error() == Some(1314)
            {
                std::fs::remove_file(outside).unwrap();
                std::fs::remove_dir(root).unwrap();
                std::fs::remove_dir(base).unwrap();
                return;
            }
            panic!("could not create test source link: {error}");
        }
        #[cfg(unix)]
        std::os::unix::fs::symlink(&outside, &link).unwrap();
        let session: crate::Session = serde_json::from_value(j!({
            "id":"link","scope":"synthetic:link","source_root":root,
        }))
        .unwrap();
        let error = SourceReader::new(&session)
            .unwrap()
            .read("linked.md")
            .unwrap_err();
        assert_eq!(error.to_string(), "source link rejected");

        std::fs::remove_file(link).unwrap();
        std::fs::remove_file(outside).unwrap();
        std::fs::remove_dir(root).unwrap();
        std::fs::remove_dir(base).unwrap();
    }

    #[test]
    fn purge_references_are_indexed_after_initialise_and_upgrade() {
        let db = Database::initialize(rusqlite::Connection::open_in_memory().unwrap()).unwrap();
        Knowledge::initialize(&db).unwrap();
        let references = [
            ("cortex_memory", "supersedes_id", "native_memory_supersedes"),
            (
                "cortex_verbatim",
                "linked_memory_id",
                "native_verbatim_link",
            ),
            ("knowledge_item", "owner", "native_knowledge_owner"),
            ("knowledge_item", "target", "native_knowledge_target"),
            ("native_chat_link", "memory_id", "native_chat_link_memory"),
        ];
        for upgrade in [false, true] {
            if upgrade {
                for (_, _, index) in references {
                    db.conn
                        .execute_batch(&format!("DROP INDEX {index}"))
                        .unwrap();
                }
                Knowledge::initialize(&db).unwrap();
            }
            for (table, column, index) in references {
                let query = format!("EXPLAIN QUERY PLAN SELECT * FROM {table} WHERE {column}=?");
                let details: Vec<String> = db
                    .conn
                    .prepare(&query)
                    .unwrap()
                    .query_map([vec![0u8; 16]], |row| row.get(3))
                    .unwrap()
                    .collect::<rusqlite::Result<_>>()
                    .unwrap();
                assert!(
                    details.iter().any(|line| line.contains(index)),
                    "{details:?}"
                );
                assert!(
                    details.iter().all(|line| !line.starts_with("SCAN ")),
                    "{details:?}"
                );
            }
            let details: Vec<String> = db
                .conn
                .prepare("EXPLAIN QUERY PLAN DELETE FROM cortex_memory WHERE memory_id=?")
                .unwrap()
                .query_map([vec![0u8; 16]], |row| row.get(3))
                .unwrap()
                .collect::<rusqlite::Result<_>>()
                .unwrap();
            for (table, _, _) in references {
                assert!(
                    details
                        .iter()
                        .all(|line| !line.starts_with(&format!("SCAN {table}"))),
                    "{details:?}"
                );
            }
        }
    }

    #[test]
    fn lexical_query_plan_starts_from_matching_terms() {
        let db = Database::initialize(rusqlite::Connection::open_in_memory().unwrap()).unwrap();
        Knowledge::initialize(&db).unwrap();
        for ranked in [false, true] {
            let query = format!("EXPLAIN QUERY PLAN {}", candidate_sql(1, 65, ranked));
            let mut statement = db.conn.prepare(&query).unwrap();
            let details: Vec<String> = statement
                .query_map(
                    params!["synthetic", now(), now(), now(), codec::term_hash("rare")],
                    |row| row.get(3),
                )
                .unwrap()
                .collect::<rusqlite::Result<_>>()
                .unwrap();
            assert!(
                details[0].contains("SEARCH t USING COVERING INDEX cortex_term_hash"),
                "{details:?}"
            );
            assert!(
                details[1].contains("SEARCH m USING INTEGER PRIMARY KEY"),
                "{details:?}"
            );
        }
    }
}
