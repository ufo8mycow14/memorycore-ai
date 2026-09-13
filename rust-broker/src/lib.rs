//! Bounded host-session protocol with a native engine and explicit reference backend.
use serde::{Deserialize, Serialize};
use serde_json::Value;
pub mod native;
pub mod scheduler;

pub const MAX_FRAME: usize = 65_536;
pub const MAX_RESPONSE: usize = 1_048_576;
pub const MAX_PENDING: usize = 32;

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Session {
    pub id: String,
    pub scope: String,
    pub source_root: String,
    #[serde(default = "yes")]
    pub use_memories: bool,
    #[serde(default = "yes")]
    pub generate_memories: bool,
    #[serde(default)]
    pub redact_secrets: bool,
    #[serde(default)]
    pub disable_on_external_context: bool,
    #[serde(default)]
    pub allow_admin: bool,
    #[serde(default)]
    pub allow_plaintext_export: bool,
}
fn yes() -> bool {
    true
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub synthetic: bool,
    #[serde(default = "native_backend")]
    pub backend: String,
    #[serde(default)]
    pub python: String,
    #[serde(default)]
    pub backend_root: String,
    pub database: String,
    pub sessions: Vec<Session>,
    pub read_workers: usize,
    #[serde(default)]
    pub key_env: Option<String>,
    #[serde(default)]
    pub allow_plaintext: bool,
}
fn native_backend() -> String {
    "native".into()
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub session: String,
    pub id: String,
    pub operation: String,
    #[serde(deserialize_with = "unique_arguments")]
    pub arguments: Value,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub recovery: Option<Recovery>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Recovery {
    pub key: String,
    pub expires_at: i64,
}

pub fn unique_arguments<'de, D: serde::Deserializer<'de>>(d: D) -> Result<Value, D::Error> {
    struct Unique(Value);
    impl<'de> Deserialize<'de> for Unique {
        fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
            struct Visitor;
            impl<'de> serde::de::Visitor<'de> for Visitor {
                type Value = Unique;
                fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                    f.write_str("JSON without duplicate keys")
                }
                fn visit_bool<E: serde::de::Error>(self, v: bool) -> Result<Unique, E> {
                    Ok(Unique(v.into()))
                }
                fn visit_i64<E: serde::de::Error>(self, v: i64) -> Result<Unique, E> {
                    Ok(Unique(v.into()))
                }
                fn visit_u64<E: serde::de::Error>(self, v: u64) -> Result<Unique, E> {
                    Ok(Unique(v.into()))
                }
                fn visit_f64<E: serde::de::Error>(self, v: f64) -> Result<Unique, E> {
                    serde_json::Number::from_f64(v)
                        .map(|n| Unique(Value::Number(n)))
                        .ok_or_else(|| E::custom("invalid number"))
                }
                fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<Unique, E> {
                    Ok(Unique(v.into()))
                }
                fn visit_unit<E: serde::de::Error>(self) -> Result<Unique, E> {
                    Ok(Unique(Value::Null))
                }
                fn visit_seq<A: serde::de::SeqAccess<'de>>(
                    self,
                    mut a: A,
                ) -> Result<Unique, A::Error> {
                    let mut values = Vec::new();
                    while let Some(Unique(v)) = a.next_element()? {
                        values.push(v);
                    }
                    Ok(Unique(Value::Array(values)))
                }
                fn visit_map<A: serde::de::MapAccess<'de>>(
                    self,
                    mut a: A,
                ) -> Result<Unique, A::Error> {
                    let mut values = serde_json::Map::new();
                    while let Some((key, Unique(v))) = a.next_entry::<String, Unique>()? {
                        if values.insert(key, v).is_some() {
                            return Err(serde::de::Error::custom("duplicate field"));
                        }
                    }
                    Ok(Unique(Value::Object(values)))
                }
            }
            d.deserialize_any(Visitor)
        }
    }
    Unique::deserialize(d).map(|v| v.0)
}

pub fn identifier(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 80
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_:".contains(&b))
}

impl Config {
    pub fn validate(&self) -> Result<(), &'static str> {
        if !["native", "python"].contains(&self.backend.as_str())
            || !std::path::Path::new(&self.database).is_absolute()
            || (self.backend == "python"
                && [&self.python, &self.backend_root]
                    .iter()
                    .any(|p| !std::path::Path::new(p).is_absolute()))
        {
            return Err("absolute host paths required");
        }
        if !self.synthetic
            || !(1..=8).contains(&self.read_workers)
            || self.sessions.is_empty()
            || self.sessions.len() > 128
        {
            return Err("invalid synthetic host configuration");
        }
        let mut ids = std::collections::HashSet::new();
        if (self.key_env.is_none() && !self.allow_plaintext)
            || (self.key_env.is_some() && self.allow_plaintext)
        {
            return Err("select an encryption key or explicitly allow synthetic plaintext");
        }
        if let Some(key) = &self.key_env
            && (self.backend != "native"
                || key.is_empty()
                || !key.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_'))
        {
            return Err("invalid encryption key source");
        }
        for s in &self.sessions {
            if !identifier(&s.id)
                || !ids.insert(&s.id)
                || s.scope.is_empty()
                || !std::path::Path::new(&s.source_root).is_absolute()
            {
                return Err("invalid or duplicate host session");
            }
        }
        Ok(())
    }
}

pub fn validate_request(r: &Request) -> bool {
    identifier(&r.id)
        && identifier(&r.session)
        && r.recovery
            .as_ref()
            .is_none_or(|v| identifier(&v.key) && !is_read(r))
        && r.arguments.is_object()
        && matches!(
            r.operation.as_str(),
            "ping"
                | "catalogue"
                | "call"
                | "background-propose"
                | "routing-plan"
                | "routing-recall"
                | "routing-export"
                | "routing-calibrate"
                | "routing-checkpoint"
                | "routing-prepare"
                | "routing-handoff"
                | "routing-cancel"
                | "admin"
        )
}

pub fn is_read(r: &Request) -> bool {
    match r.operation.as_str() {
        "admin" => native::admin::is_read(&r.arguments),
        "ping" | "catalogue" | "routing-plan" | "routing-recall" | "routing-export"
        | "routing-calibrate" => true,
        "call" => {
            let name = r
                .arguments
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or("");
            let a = r.arguments.get("arguments").and_then(Value::as_object);
            let read = [
                "recall",
                "review",
                "freshness",
                "relations",
                "graph",
                "review_forget",
                "code_context",
            ];
            if name == "memory" {
                let all = [
                    "recall",
                    "propose",
                    "review",
                    "accept",
                    "reject",
                    "freshness",
                    "relations",
                    "graph",
                    "review_forget",
                    "forget",
                    "code_context",
                ];
                if let Some(a) = a {
                    let actions: Vec<_> = all.iter().filter(|k| a.contains_key(**k)).collect();
                    actions.len() == 1 && read.contains(actions[0])
                } else {
                    false
                }
            } else {
                read.contains(&name.strip_prefix("memory_").unwrap_or(name))
            }
        }
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn request(arguments: Value) -> Request {
        Request {
            session: "chat-1".into(),
            id: "1".into(),
            operation: "call".into(),
            arguments,
            recovery: None,
        }
    }
    #[test]
    fn reads_and_writes_are_separate() {
        for action in [
            "recall",
            "review",
            "freshness",
            "relations",
            "graph",
            "review_forget",
        ] {
            assert!(is_read(&request(
                json!({"name":"memory","arguments":{action:"x"}})
            )));
            assert!(is_read(&request(
                json!({"name":format!("memory_{action}"),"arguments":{}})
            )));
        }
        for action in ["propose", "accept", "reject", "forget", "unknown"] {
            assert!(!is_read(&request(
                json!({"name":"memory","arguments":{action:"x"}})
            )));
        }
        assert!(!is_read(&request(
            json!({"name":"memory","arguments":{"recall":"x","forget":"y"}})
        )));
    }
    #[test]
    fn rejects_protocol_overrides_and_duplicate_keys() {
        assert!(
            serde_json::from_str::<Request>(
                r#"{"session":"a","id":"1","id":"2","operation":"ping","arguments":{}}"#
            )
            .is_err()
        );
        assert!(!validate_request(&request(json!(null))));
        assert!(!identifier("../other"));
        assert!(serde_json::from_str::<Request>(r#"{"session":"a","id":"1","operation":"call","arguments":{"name":"memory","arguments":{"recall":"a","recall":"b"}}}"#).is_err());
    }
    #[test]
    fn observation_floats_survive_request_roundtrip() {
        for text in [
            "1788790751.9037495",
            "1788790752.6678727",
            "1788790753.2714741",
        ] {
            let raw = format!(
                r#"{{"session":"a","id":"1","operation":"routing","arguments":{{"observed_at":{text}}}}}"#
            );
            let request: Request = serde_json::from_str(&raw).unwrap();
            assert_eq!(
                request.arguments["observed_at"].as_f64().unwrap().to_bits(),
                text.parse::<f64>().unwrap().to_bits()
            );
            let encoded = serde_json::to_string(&request).unwrap();
            let decoded: Request = serde_json::from_str(&encoded).unwrap();
            assert_eq!(decoded.arguments, request.arguments);
        }
    }
    #[test]
    fn rejects_duplicate_sessions_and_production() {
        let mut c: Config = serde_json::from_value(json!({"synthetic":true,"allow_plaintext":true,"python":"p","backend_root":"r","database":"d","read_workers":4,
            "sessions":[{"id":"a","scope":"s","source_root":"r"},{"id":"a","scope":"s","source_root":"r"}]})).unwrap();
        let root = std::env::current_dir()
            .unwrap()
            .to_string_lossy()
            .into_owned();
        c.python = root.clone();
        c.backend_root = root.clone();
        c.database = root.clone();
        for session in &mut c.sessions {
            session.source_root = root.clone();
        }
        assert!(c.validate().is_err());
        c.sessions.pop();
        assert!(c.validate().is_ok());
        c.synthetic = false;
        assert!(c.validate().is_err());
    }
    #[test]
    fn supports_one_hundred_sessions_with_a_hard_upper_bound() {
        let root = std::env::current_dir()
            .unwrap()
            .to_string_lossy()
            .into_owned();
        let sessions: Vec<Value>=(0..128).map(|n|json!({"id":format!("chat-{n}"),"scope":format!("project-{}",n%10),"source_root":root})).collect();
        let mut config: Config=serde_json::from_value(json!({"synthetic":true,"allow_plaintext":true,"database":root,"read_workers":4,"sessions":sessions})).unwrap();
        assert!(config.validate().is_ok());
        let mut excess = config.sessions[0].clone();
        excess.id = "excess".into();
        config.sessions.push(excess);
        assert!(config.validate().is_err());
    }
}
