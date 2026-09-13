use super::{Result, ensure};
use std::collections::BTreeSet;
use std::io::{Read, Write};
pub const MAX_DECOMPRESSED: usize = 8 * 1024 * 1024 + 65536;

pub fn compress(raw: &[u8]) -> Result<Vec<u8>> {
    let mut z = flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::best());
    z.write_all(raw)?;
    let zipped = z.finish()?;
    let (marker, data) = if zipped.len() < raw.len() {
        (b'Z', zipped.as_slice())
    } else {
        (b'N', raw)
    };
    let mut result = vec![marker];
    result.extend_from_slice(data);
    Ok(result)
}

pub fn decompress(blob: &[u8]) -> Result<Vec<u8>> {
    if blob.is_empty() {
        return Ok(Vec::new());
    }
    match blob[0] {
        b'N' => {
            ensure(blob.len() - 1 <= MAX_DECOMPRESSED, "decompression limit")?;
            Ok(blob[1..].to_vec())
        }
        b'Z' => {
            let mut decoder = flate2::bufread::ZlibDecoder::new(&blob[1..]);
            let mut output = Vec::new();
            decoder
                .by_ref()
                .take((MAX_DECOMPRESSED + 1) as u64)
                .read_to_end(&mut output)?;
            ensure(
                output.len() <= MAX_DECOMPRESSED && decoder.total_in() == (blob.len() - 1) as u64,
                "invalid compressed boundaries",
            )?;
            // A truncated stream can yield bytes without an I/O error. Require
            // an explicit end-of-stream from the low-level decoder as well.
            let mut check = flate2::Decompress::new(true);
            let mut verified = vec![0; output.len() + 1];
            let status =
                check.decompress(&blob[1..], &mut verified, flate2::FlushDecompress::Finish)?;
            ensure(
                status == flate2::Status::StreamEnd && check.total_in() == (blob.len() - 1) as u64,
                "incomplete compressed stream",
            )?;
            Ok(output)
        }
        _ => Err("unknown compression marker".into()),
    }
}

pub fn varint(mut value: u64, out: &mut Vec<u8>) {
    loop {
        let b = (value & 127) as u8;
        value >>= 7;
        out.push(b | if value != 0 { 128 } else { 0 });
        if value == 0 {
            break;
        }
    }
}

pub fn read_varint(raw: &[u8], offset: &mut usize) -> Result<u64> {
    let mut result = 0u64;
    for shift in (0..=63).step_by(7) {
        let b = *raw.get(*offset).ok_or("truncated varint")?;
        *offset += 1;
        ensure(shift != 63 || b <= 1, "varint overflow")?;
        result |= u64::from(b & 127) << shift;
        if b & 128 == 0 {
            return Ok(result);
        }
    }
    Err("invalid varint".into())
}

pub fn encode(fields: [&str; 5]) -> Result<Vec<u8>> {
    let mut raw = b"BM1".to_vec();
    for f in fields {
        let f = f.trim().as_bytes();
        varint(f.len() as u64, &mut raw);
        raw.extend_from_slice(f);
    }
    compress(&raw)
}

pub fn decode(blob: &[u8]) -> Result<[String; 5]> {
    let raw = decompress(blob)?;
    ensure(raw.starts_with(b"BM1"), "invalid wire version")?;
    let mut offset = 3;
    let mut fields = Vec::new();
    for _ in 0..5 {
        let len = usize::try_from(read_varint(&raw, &mut offset)?)?;
        let end = offset.checked_add(len).ok_or("invalid field length")?;
        fields
            .push(std::str::from_utf8(raw.get(offset..end).ok_or("truncated field")?)?.to_owned());
        offset = end;
    }
    ensure(offset == raw.len(), "trailing wire data")?;
    fields.try_into().map_err(|_| "invalid wire fields".into())
}

pub fn tokenize(value: &str) -> BTreeSet<String> {
    let clean: String = value
        .chars()
        .flat_map(|c| {
            if c.is_alphanumeric() {
                c.to_lowercase().collect::<Vec<_>>()
            } else {
                vec![' ']
            }
        })
        .collect();
    let mut words: BTreeSet<String> = clean
        .split_whitespace()
        .filter(|s| s.chars().count() > 1)
        .map(str::to_owned)
        .collect();
    for word in words.clone() {
        if word.len() > 4 && word.is_ascii() && word.chars().all(char::is_alphabetic) {
            if let Some(stem) = word.strip_suffix("ies") {
                words.insert(format!("{stem}y"));
            } else if word.ends_with('s') && !["ss", "us", "is"].iter().any(|s| word.ends_with(s)) {
                words.insert(word[..word.len() - 1].into());
            }
        }
    }
    for stop in [
        "the", "and", "for", "with", "from", "this", "that", "what", "where", "does", "are", "is",
        "of", "to", "in",
    ] {
        words.remove(stop);
    }
    words
}

pub fn term_hash(token: &str) -> Vec<u8> {
    blake2b_simd::Params::new()
        .hash_length(8)
        .personal(b"BMemTerm")
        .hash(token.as_bytes())
        .as_bytes()
        .to_vec()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn roundtrips_and_strict_boundaries() {
        for summary in ["short".into(), "long summary ".repeat(100)] {
            let value = encode(["subject", &summary, "detail", "keys", "source"]).unwrap();
            assert_eq!(decode(&value).unwrap()[1], summary.trim());
            let mut trailing = value.clone();
            trailing.push(1);
            assert!(decode(&trailing).is_err());
            assert!(decode(&value[..value.len() - 1]).is_err());
        }
        let mut offset = 0;
        assert!(read_varint(&[255; 11], &mut offset).is_err());
    }
    #[test]
    fn bounded_decompression() {
        let blob = compress(&vec![b'a'; MAX_DECOMPRESSED + 1]).unwrap();
        assert!(decompress(&blob).is_err());
    }
}
