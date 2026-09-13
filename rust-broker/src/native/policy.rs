use super::{Result, ensure, json};
use regex::Regex;
use serde_json::Value;
use std::sync::OnceLock;

pub const MAX_TEXT: usize = 1024 * 1024;
pub const MAX_EXACT: usize = 8 * 1024 * 1024;

fn patterns() -> &'static Vec<Regex> {
    static RULES: OnceLock<Vec<Regex>> = OnceLock::new();
    RULES.get_or_init(|| [
        r"-----BEGIN (?:[A-Z0-9 ]* )?PRIVATE KEY-----",
        r#"\b(?:password|passphrase|passwd|api[_ -]?key|client[_ -]?secret|access[_ -]?token|refresh[_ -]?token|session[_ -]?(?:token|cookie)|csrf[_ -]?token|otp|totp|pin|cvv|cvc|bsb|iban|routing[_ -]?number|bank[_ -]?account)\s*["']?\s*[:=]\s*[^\s,;}]+"#,
        r"\b(?:authorization\s*:\s*)?bearer\s+[A-Za-z0-9._~+/=-]{8,}",
        r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b",
        r"https?://[^\s/@:]+:[^\s/@]+@|[?&](?:token|key|secret|code|password)=[^\s&#]+",
    ].into_iter().map(|p| Regex::new(&format!("(?i){p}")).expect("constant policy regex")).collect())
}

fn check_shape(text: &str, max: usize) -> Result<()> {
    ensure(
        text.len() <= max
            && !text
                .chars()
                .any(|c| (c as u32) < 32 && !"\n\r\t".contains(c)),
        "invalid text size or controls",
    )
}

pub fn check_text(text: &str, max: usize) -> Result<()> {
    check_shape(text, max)?;
    ensure(
        !patterns().iter().any(|r| r.is_match(text)),
        "possible sensitive input",
    )?;
    static IBAN: OnceLock<Regex> = OnceLock::new();
    for m in IBAN
        .get_or_init(|| Regex::new(r"\b[A-Z]{2}[0-9]{2}(?: ?[A-Z0-9]){11,30}\b").unwrap())
        .find_iter(text)
    {
        let iban = m.as_str().replace(' ', "");
        let len = match &iban[..2] {
            "DE" => 22,
            "BE" => 16,
            "FR" => 27,
            "GB" => 22,
            "ES" => 24,
            "IT" => 27,
            "NL" => 18,
            "CH" => 21,
            "AT" => 20,
            "IE" => 22,
            "PL" => 28,
            "PT" => 25,
            _ => 0,
        };
        if iban.len() == len {
            let mut rem = 0u32;
            for b in iban[4..].bytes().chain(iban[..4].bytes()) {
                if b.is_ascii_uppercase() {
                    rem = (rem * 100 + u32::from(b - b'A' + 10)) % 97;
                } else {
                    rem = (rem * 10 + u32::from(b - b'0')) % 97;
                }
            }
            ensure(rem != 1, "possible bank identifier")?;
        }
    }
    // Scan numeric runs and retain the original fingerprint exemption.
    static UNICODE_NUMBERS: OnceLock<Regex> = OnceLock::new();
    for m in UNICODE_NUMBERS
        .get_or_init(|| Regex::new(r"(?:\p{Nd}[ -]?){13,19}").unwrap())
        .find_iter(text)
    {
        ensure(m.as_str().is_ascii(), "non-ASCII account-shaped number")?;
    }
    static NUMBERS: OnceLock<Regex> = OnceLock::new();
    static HEX: OnceLock<Regex> = OnceLock::new();
    let hex = HEX.get_or_init(|| Regex::new(r"\b[a-fA-F0-9]{32}(?:[a-fA-F0-9]{32})?\b").unwrap());
    for m in NUMBERS
        .get_or_init(|| Regex::new(r"(?:[0-9][ -]?){13,19}").unwrap())
        .find_iter(text)
    {
        if text[..m.start()]
            .chars()
            .next_back()
            .is_some_and(|c| c.is_ascii_digit())
            || text[m.end()..]
                .chars()
                .next()
                .is_some_and(|c| c.is_ascii_digit())
        {
            continue;
        }
        let end = m.start() + m.as_str().trim_end_matches([' ', '-']).len();
        if hex.find_iter(text).any(|h| {
            h.start() <= m.start()
                && end <= h.end()
                && h.as_str().bytes().any(|b| b.is_ascii_alphabetic())
        }) {
            continue;
        }
        let digits: Vec<u32> = m
            .as_str()
            .bytes()
            .filter(u8::is_ascii_digit)
            .map(|b| u32::from(b - b'0'))
            .collect();
        let sum: u32 = digits
            .iter()
            .rev()
            .enumerate()
            .map(|(i, &d)| {
                if i % 2 == 0 {
                    d
                } else if d >= 5 {
                    2 * d - 9
                } else {
                    2 * d
                }
            })
            .sum();
        ensure(!sum.is_multiple_of(10), "possible payment account")?;
    }
    Ok(())
}

pub fn check_exact(raw: &[u8], media_type: &str) -> Result<()> {
    ensure(
        !raw.is_empty() && raw.len() <= MAX_EXACT,
        "invalid exact size",
    )?;
    let kind = media_type
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_lowercase();
    ensure(
        ["text/plain", "text/markdown", "application/json"].contains(&kind.as_str()),
        "unsupported exact format",
    )?;
    let text = std::str::from_utf8(raw)?;
    check_text(text, MAX_EXACT)?;
    if kind == "application/json" {
        let value = json(text)?;
        let mut pending = vec![(&value, 0)];
        while let Some((v, depth)) = pending.pop() {
            ensure(depth <= 32, "JSON nesting limit")?;
            match v {
                Value::Object(map) => {
                    for (key, item) in map {
                        check_text(key, MAX_TEXT)?;
                        check_text(&format!("{key}=present"), MAX_EXACT)?;
                        pending.push((item, depth + 1));
                    }
                }
                Value::Array(items) => {
                    for item in items {
                        pending.push((item, depth + 1));
                    }
                }
                Value::String(s) => check_text(s, MAX_EXACT)?,
                _ => {}
            }
        }
    }
    Ok(())
}

pub fn redact(text: &str) -> Result<(String, usize)> {
    check_shape(text, MAX_TEXT)?;
    static EMPTY: OnceLock<Regex> = OnceLock::new();
    ensure(!EMPTY.get_or_init(||Regex::new(r#"(?im)\b(?:password|passphrase|passwd|api[_ -]?key|client[_ -]?secret|access[_ -]?token|refresh[_ -]?token|session[_ -]?(?:token|cookie))[ \t]*["']?[ \t]*[:=][ \t]*$"#).unwrap()).is_match(text),"ambiguous multiline sensitive value")?;
    static BEGIN: OnceLock<Regex> = OnceLock::new();
    let begin =
        BEGIN.get_or_init(|| Regex::new(r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY)-----").unwrap());
    let mut projected = text.to_owned();
    let mut count = 0;
    while let Some(c) = begin.captures(&projected) {
        let m = c.get(0).unwrap();
        let marker = format!("-----END {}-----", &c[1]);
        let end = projected[m.end()..]
            .find(&marker)
            .map(|n| m.end() + n + marker.len())
            .ok_or("incomplete private key")?;
        projected.replace_range(m.start()..end, "[REDACTED PRIVATE KEY]");
        count += 1;
    }
    let lines: Vec<&str> = projected.split_inclusive('\n').collect();
    let mut out = String::new();
    for (i, line) in lines.iter().enumerate() {
        if check_text(line, MAX_TEXT).is_ok() {
            out.push_str(line);
            continue;
        }
        ensure(
            !patterns()[0].is_match(line) && patterns()[1..].iter().any(|r| r.is_match(line)),
            "source cannot be redacted",
        )?;
        let next = lines.get(i + 1).copied().unwrap_or("");
        ensure(
            line.matches('"').count() % 2 == 0
                && line.matches('\'').count() % 2 == 0
                && !["\\", "|", ">", "|-", ">-", "|+", ">+"]
                    .iter()
                    .any(|s| line.trim_end().ends_with(s))
                && (next.trim().is_empty() || !next.starts_with(char::is_whitespace)),
            "ambiguous multiline sensitive value",
        )?;
        out.push_str("[REDACTED SENSITIVE LINE]");
        if line.ends_with('\n') {
            out.push('\n');
        }
        count += 1;
    }
    check_text(&out, MAX_TEXT)?;
    Ok((out, count))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_credentials_and_decoded_keys() {
        for text in [
            "password=synthetic",
            "Authorization: Bearer synthetictoken",
            "4111 1111 1111 1111",
            "GB82 WEST 1234 5698 7654 32",
        ] {
            assert!(check_text(text, MAX_TEXT).is_err(), "{text}");
        }
        assert!(check_exact(br#"{"pass\u0077ord":null}"#, "application/json").is_err());
        assert!(check_exact(br#"{"x":1,"x":2}"#, "application/json").is_err());
    }
    #[test]
    fn redaction_preserves_exceptions_and_rejects_ambiguity() {
        let (out, n) = redact("Fact: Synthetic.\npassword=synthetic\nNever publish.\n").unwrap();
        assert_eq!(n, 1);
        assert!(out.contains("Never publish."));
        assert!(!out.contains("password="));
        assert!(redact("password=\ncontinued").is_err());
        assert!(redact("password: |\n  secret").is_err());
        assert!(redact("-----BEGIN PRIVATE KEY-----\nunfinished").is_err());
    }
}
