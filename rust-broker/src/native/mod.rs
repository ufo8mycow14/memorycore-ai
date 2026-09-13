//! Native engine components; each port is checked against the reference engine.
pub mod admin;
mod answerability;
pub mod backup;
pub mod chat_lifecycle;
mod checkpoint;
pub mod codec;
pub mod database;
pub mod exact;
pub mod generation;
pub mod graph;
pub mod knowledge;
pub mod policy;
pub mod portable;
pub mod recovery;
pub mod retention;
pub mod routing;
pub mod service;
pub mod vectors;
pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;

pub fn ensure(condition: bool, message: &'static str) -> Result<()> {
    if condition {
        Ok(())
    } else {
        Err(message.into())
    }
}

pub fn canonical(value: &serde_json::Value, ascii: bool) -> Result<String> {
    let text = serde_json::to_string(value)?;
    if !ascii {
        return Ok(text);
    }
    let mut out = String::new();
    for c in text.chars() {
        if c <= '\u{007e}' {
            out.push(c);
        } else {
            for unit in c.encode_utf16(&mut [0; 2]) {
                out.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    Ok(out)
}

pub fn json(raw: &str) -> Result<serde_json::Value> {
    let mut de = serde_json::Deserializer::from_str(raw);
    let value = crate::unique_arguments(&mut de)?;
    de.end()?;
    Ok(value)
}

pub fn sha(raw: &[u8]) -> Vec<u8> {
    use sha2::{Digest, Sha256};
    Sha256::digest(raw).to_vec()
}

pub fn now() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, false)
}

pub fn timestamp(text: &str) -> Result<String> {
    Ok(chrono::DateTime::parse_from_rfc3339(text)?
        .with_timezone(&chrono::Utc)
        .to_rfc3339_opts(chrono::SecondsFormat::Secs, false))
}

pub fn after_days(days: i64, base: &str) -> Result<String> {
    let value = chrono::DateTime::parse_from_rfc3339(base)?
        .checked_add_signed(chrono::Duration::try_days(days).ok_or("invalid interval")?)
        .ok_or("invalid interval")?;
    Ok(value
        .with_timezone(&chrono::Utc)
        .to_rfc3339_opts(chrono::SecondsFormat::Secs, false))
}
