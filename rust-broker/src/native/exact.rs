use super::{
    Result, codec,
    database::{Database, Row, boolean, field, identifier, optional_time, text},
    ensure, now, policy, sha,
};
use rusqlite::{params, types::Value as Sql};
use serde_json::{Value, json};
use std::collections::BTreeMap;

impl Database {
    pub fn exact_bytes(&self, scope: &str, id: &str) -> Result<(Row, Vec<u8>)> {
        let row = self.row("cortex_verbatim", "archive_id", &identifier(id)?)?;
        ensure(
            row.text("scope")? == scope
                && row.int("status")? == 0
                && row
                    .optional("expires_at")?
                    .is_none_or(|s| s > now().as_str()),
            "exact record unavailable",
        )?;
        let raw = verify(&row)?;
        Ok((row, raw))
    }
    pub fn store_exact(&self, scope: &str, args: &Value) -> Result<Value> {
        ensure(
            boolean(args, "user_confirmed", false)?,
            "exact storage needs confirmation",
        )?;
        let raw = field(args, "text")?.as_bytes();
        let media = text(args, "media_type", "text/plain")?;
        policy::check_exact(raw, media)?;
        let source = text(args, "source", "user")?;
        policy::check_text(source, policy::MAX_TEXT)?;
        let retention = text(args, "retention", "until_user_deletes")?;
        let expires = optional_time(args, "expires")?;
        ensure(
            ["until_user_deletes", "expiring"].contains(&retention)
                && (retention == "expiring") == expires.is_some(),
            "invalid exact retention",
        )?;
        let pin = retention == "until_user_deletes" || boolean(args, "pinned", false)?;
        let mut identity = scope.as_bytes().to_vec();
        identity.push(0);
        identity.extend_from_slice(media.as_bytes());
        identity.push(0);
        identity.extend_from_slice(raw);
        let id = blake2b_simd::Params::new()
            .hash_length(16)
            .personal(b"BMemExact")
            .hash(&identity)
            .as_bytes()
            .to_vec();
        ensure(
            self.conn.query_row(
                "SELECT count(*) FROM cortex_tombstone WHERE memory_id=?",
                [&id],
                |r| r.get::<_, i64>(0),
            )? == 0,
            "purged exact identifier",
        )?;
        let existing = self.rows(
            "SELECT * FROM cortex_verbatim WHERE archive_id=?",
            vec![Sql::Blob(id.clone())],
        )?;
        let created = now();
        if let Some(row) = existing.first() {
            ensure(
                verify(row)? == raw
                    && row.text("scope")? == scope
                    && row.int("status")? == 0
                    && row
                        .optional("expires_at")?
                        .is_none_or(|s| s > created.as_str()),
                "inactive or corrupt duplicate",
            )?;
            ensure(
                row.text("retention")? == retention
                    && row.optional("expires_at")? == expires.as_deref()
                    && (row.int("pinned")? != 0) == pin
                    && row.text("source")? == source,
                "duplicate metadata differs",
            )?;
        } else {
            let blob = codec::compress(raw)?;
            let mut v = BTreeMap::new();
            for (k, s) in [
                ("scope", scope),
                ("media_type", media),
                ("source", source),
                ("created_at", &created),
                ("updated_at", &created),
                ("retention", retention),
            ] {
                v.insert(k.into(), Sql::Text(s.into()));
            }
            for (k, b) in [
                ("archive_id", id.clone()),
                ("original_blob", blob.clone()),
                ("content_sha256", sha(raw)),
            ] {
                v.insert(k.into(), Sql::Blob(b));
            }
            for (k, n) in [
                ("original_bytes", raw.len() as i64),
                ("stored_bytes", blob.len() as i64),
                ("pinned", i64::from(pin)),
            ] {
                v.insert(k.into(), Sql::Integer(n));
            }
            v.insert(
                "expires_at".into(),
                expires.clone().map(Sql::Text).unwrap_or(Sql::Null),
            );
            self.insert("cortex_verbatim", v)?;
        }
        if let Some(Value::String(link)) = args.get("linked_memory_id") {
            self.memory(scope, link, true)?;
            self.conn.execute(
                "UPDATE cortex_verbatim SET linked_memory_id=? WHERE archive_id=?",
                params![identifier(link)?, id],
            )?;
        }
        self.seal("cortex_verbatim", "archive_id", &id)?;
        let row = self.row("cortex_verbatim", "archive_id", &id)?;
        verify(&row)?;
        Ok(
            json!({"archive_id":hex::encode(&id),"original_bytes":raw.len(),"content_sha256":hex::encode(sha(raw)),"scope":scope,"media_type":media,"stored_at":row.text("created_at")?,"retention":retention,"expires_at":expires,"pinned":pin,"status":"active","linked_memory_id":match row.0.get("linked_memory_id"){Some(Sql::Blob(b))=>json!(hex::encode(b)),_=>Value::Null},"encryption":if self.encrypted(){"SQLCIPHER"}else{"UNENCRYPTED_PROTOTYPE"},"verified":true,"deduplicated":!existing.is_empty()}),
        )
    }
}
pub fn verify(row: &Row) -> Result<Vec<u8>> {
    row.verify()?;
    let raw = codec::decompress(row.bytes("original_blob")?)?;
    policy::check_exact(&raw, row.text("media_type")?)?;
    ensure(
        sha(&raw) == row.bytes("content_sha256")?
            && row.int("original_bytes")? == raw.len() as i64
            && row.int("stored_bytes")? == row.bytes("original_blob")?.len() as i64,
        "exact integrity failure",
    )?;
    ensure(
        ["until_user_deletes", "expiring"].contains(&row.text("retention")?)
            && (row.text("retention")? == "expiring") == row.optional("expires_at")?.is_some(),
        "invalid retention",
    )?;
    policy::check_text(row.text("source")?, policy::MAX_TEXT)?;
    Ok(raw)
}
