//! Expiring, atomic retry receipts. Expired keys are rejected, never re-executed.
use super::{Result, canonical, database::Database, ensure, json, knowledge::digest};
use crate::{Request, Session};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json as j};

const MAX_RECEIPTS: i64 = 4096;
const MAX_RECEIPT_BYTES: usize = 16 * 1024;

pub fn lookup(db: &Database, s: &Session, r: &Request) -> Result<Option<Value>> {
    let Some(recovery) = &r.recovery else {
        return Ok(None);
    };
    let now = chrono::Utc::now().timestamp();
    ensure(
        recovery.expires_at > now && recovery.expires_at <= now + 86400,
        "retry window must be in the next 24 hours",
    )?;
    db.conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS native_retry_receipt(
        scope TEXT NOT NULL,key TEXT NOT NULL,fingerprint TEXT NOT NULL,
        expires_at INTEGER NOT NULL,response TEXT NOT NULL,checksum TEXT NOT NULL,
        PRIMARY KEY(scope,key));
        CREATE INDEX IF NOT EXISTS native_retry_expiry ON native_retry_receipt(expires_at);",
    )?;
    // Cleanup is inside the same write transaction as the mutation and receipt.
    db.conn.execute(
        "DELETE FROM native_retry_receipt WHERE expires_at<=?",
        [now],
    )?;
    let stored = db.conn.query_row(
        "SELECT fingerprint,response,checksum FROM native_retry_receipt WHERE scope=? AND key=?",
        params![s.scope, recovery.key], |row| Ok((row.get::<_,String>(0)?,row.get::<_,String>(1)?,row.get::<_,String>(2)?))
    ).optional()?;
    if let Some((fingerprint, response, checksum)) = stored {
        ensure(
            fingerprint == identity(s, r)?,
            "retry identity collision or policy changed",
        )?;
        ensure(
            !response.is_empty(),
            "retry receipt revoked after purge; mutation will not be replayed",
        )?;
        let response = json(&response)?;
        ensure(
            digest(&response)? == checksum,
            "retry receipt integrity failure",
        )?;
        return Ok(Some(response));
    }
    let n: i64 = db
        .conn
        .query_row("SELECT count(*) FROM native_retry_receipt", [], |row| {
            row.get(0)
        })?;
    ensure(
        n < MAX_RECEIPTS,
        "retry receipt capacity reached; no mutation performed",
    )?;
    Ok(None)
}

pub fn revoke_scope(db: &Database, scope: &str) -> Result<()> {
    let exists: bool = db.conn.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_retry_receipt')",[],|r|r.get(0))?;
    if exists {
        db.conn.execute(
            "UPDATE native_retry_receipt SET response='',checksum='' WHERE scope=?",
            [scope],
        )?;
    }
    Ok(())
}

fn identity(s: &Session, r: &Request) -> Result<String> {
    digest(&j!({"session_policy":s,"request":r}))
}

pub fn record(db: &Database, s: &Session, r: &Request, response: &Value) -> Result<()> {
    let Some(recovery) = &r.recovery else {
        return Ok(());
    };
    let body = canonical(response, false)?;
    ensure(
        body.len() <= MAX_RECEIPT_BYTES,
        "retry receipt exceeds bound; mutation rolled back",
    )?;
    db.conn.execute(
        "INSERT INTO native_retry_receipt VALUES(?,?,?,?,?,?)",
        params![
            s.scope,
            recovery.key,
            identity(s, r)?,
            recovery.expires_at,
            body,
            digest(response)?
        ],
    )?;
    Ok(())
}
