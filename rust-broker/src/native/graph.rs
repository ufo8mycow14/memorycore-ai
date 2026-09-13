//! Evidence-preserving local graph packets, complementary to semantic seed recall.
use super::{
    Result, canonical,
    database::{field, identifier},
    ensure, json,
    knowledge::{Knowledge, SourceReader, digest, tokens},
};
use rusqlite::params;
use serde_json::{Value, json as j};
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    time::{Duration, Instant},
};

const MAX_NODES: usize = 16;
const MAX_INSPECTED: usize = 32;
const MAX_EDGES: usize = 64;
const MAX_SOURCES: i64 = 4;

fn node(
    k: &Knowledge,
    id: &str,
    reader: &Result<SourceReader<'_>>,
    cache: &mut BTreeMap<String, String>,
) -> Result<Value> {
    let (row, fields) = k.db.memory(&k.session.scope, id, true)?;
    let (count, bytes): (i64, i64) = k.db.conn.query_row(
        "SELECT count(*),coalesce(max(length(CAST(payload AS BLOB))),0) FROM (SELECT payload FROM knowledge_item WHERE scope=? AND owner=? AND kind='source' LIMIT ?)",
        params![k.session.scope, identifier(id)?, MAX_SOURCES + 1], |r| Ok((r.get(0)?,r.get(1)?)))?;
    ensure(
        count > 0 && count <= MAX_SOURCES && bytes <= 16384,
        "graph source bound",
    )?;
    let sources = k.source_items(&[id.into()])?;
    let freshness = k.freshness_with(id, &sources, reader, cache)?;
    ensure(freshness["state"] == "fresh", "graph source not fresh")?;
    Ok(j!({"id":id,"subject":fields[0],"summary":fields[1],
        "confidence":row.int("confidence")?,"confidence_reason":row.text("confidence_reason")?,
        "observed_at":row.optional("observed_at")?,"valid_from":row.optional("valid_from")?,
        "valid_to":row.optional("valid_to")?,"expires_at":row.optional("expires_at")?,"type":super::knowledge::memory_type(row.int("memory_type")?)?,"sources":freshness["sources"]}))
}

fn follows(intent: &str, relation: &str, outgoing: bool) -> bool {
    match intent {
        "dependencies" => outgoing && relation == "depends_on",
        "impact" => !outgoing && ["depends_on", "applies_to"].contains(&relation),
        "evidence" => outgoing && relation == "supported_by",
        "conflicts" => relation == "contradicts",
        "related" => true,
        _ => false,
    }
}

pub fn recall(
    k: &Knowledge,
    root: &str,
    intent: &str,
    depth: usize,
    budget: usize,
) -> Result<Value> {
    ensure(k.session.use_memories, "memory use disabled")?;
    identifier(root)?;
    ensure(
        ["dependencies", "impact", "evidence", "conflicts", "related"].contains(&intent),
        "invalid graph intent",
    )?;
    ensure(
        (1..=3).contains(&depth) && (256..=1240).contains(&budget),
        "invalid graph bound",
    )?;
    // Reject foreign/inactive roots without returning any associated graph metadata.
    k.db.memory(&k.session.scope, root, true)?;
    // Older portable vaults must remain readable without a schema write.
    let indexed: bool = k.db.conn.query_row(
        "SELECT count(*)=2 FROM sqlite_master WHERE type='index' AND name IN ('knowledge_graph_owner','knowledge_graph_target')",
        [], |row| row.get(0))?;
    let started = Instant::now();
    let mut packet = j!({"format":"memory-graph/1","scope":k.session.scope,"intent":intent,"depth":depth,
        "data_only":true,"inferred_truth":false,"confidence_scale":255,
        "nodes":[],"edges":[],"truncated":false,"omitted":false});
    let mut source_cache = BTreeMap::new();
    let source_reader = SourceReader::new(k.session);
    let root_node = match node(k, root, &source_reader, &mut source_cache) {
        Ok(value) => value,
        Err(_) => {
            packet["omitted"] = j!(true);
            return Ok(packet);
        }
    };
    packet["nodes"] = j!([root_node]);
    // Reserve room for final flags; never shorten a fact, evidence or qualifier.
    if tokens(&canonical(&packet, false)?) + 8 > budget {
        packet["nodes"] = j!([]);
        packet["truncated"] = j!(true);
        return Ok(packet);
    }
    let mut admitted = BTreeMap::from([(root.to_owned(), 0usize)]);
    let mut inspected = BTreeSet::from([root.to_owned()]);
    let mut eligible = BTreeMap::<String, Value>::new();
    let mut seen_edges = BTreeSet::new();
    let mut seen_links = BTreeSet::new();
    let mut frontier = VecDeque::from([(root.to_owned(), 0usize)]);
    let mut scanned = 0usize;
    'walk: while let Some((current, level)) = frontier.pop_front() {
        if level >= depth {
            continue;
        }
        if started.elapsed() >= Duration::from_millis(75) {
            packet["truncated"] = j!(true);
            break;
        }
        let remaining = MAX_EDGES - scanned;
        let relation = match intent {
            "dependencies" => " AND json_extract(payload,'$.relation')='depends_on'",
            "impact" => " AND json_extract(payload,'$.relation') IN ('depends_on','applies_to')",
            "evidence" => " AND json_extract(payload,'$.relation')='supported_by'",
            "conflicts" => " AND json_extract(payload,'$.relation')='contradicts'",
            _ => "",
        };
        // Each direction uses an ordered adjacency index. An OR with ORDER BY
        // can otherwise make SQLite scan every unrelated source in the scope.
        let directional = |column: &str| {
            let hint = if indexed {
                format!(" INDEXED BY knowledge_graph_{column}")
            } else {
                String::new()
            };
            format!(
                "SELECT id,owner,target,CASE WHEN length(CAST(payload AS BLOB))<=16384 THEN payload END,checksum FROM knowledge_item{hint} WHERE scope=?1 AND kind='relation' AND {column}=?2{relation} ORDER BY id LIMIT ?3"
            )
        };
        let sql = match intent {
            "dependencies" | "evidence" => directional("owner"),
            "impact" => directional("target"),
            _ => format!(
                "SELECT * FROM ({}) UNION SELECT * FROM ({}) ORDER BY id LIMIT ?3",
                directional("owner"),
                directional("target")
            ),
        };
        let mut statement = k.db.conn.prepare_cached(&sql)?;
        let key = identifier(&current)?;
        let rows = statement
            .query_map(params![k.session.scope, key, (remaining + 1) as i64], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, Vec<u8>>(1)?,
                    r.get::<_, Vec<u8>>(2)?,
                    r.get::<_, Option<String>>(3)?,
                    r.get::<_, String>(4)?,
                ))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        if rows.len() > remaining {
            packet["truncated"] = j!(true);
        }
        for (id, owner, target, raw, checksum) in rows.into_iter().take(remaining) {
            scanned += 1;
            if started.elapsed() >= Duration::from_millis(75) {
                packet["truncated"] = j!(true);
                break 'walk;
            }
            if !seen_edges.insert(id.clone()) {
                continue;
            }
            // Bound selected payload bytes before they cross into process memory.
            let Some(raw) = raw else {
                packet["truncated"] = j!(true);
                continue;
            };
            let owner = hex::encode(owner);
            let target = hex::encode(target);
            let edge = j!({"id":id,"scope":k.session.scope,"kind":"relation","owner":owner,"target":target,"payload":json(&raw)?});
            ensure(
                digest(&edge)? == checksum,
                "graph relation integrity failure",
            )?;
            k.validate(&edge)?;
            let relation = field(&edge["payload"], "relation")?;
            if !follows(intent, relation, owner == current) {
                continue;
            }
            let next = if owner == current { &target } else { &owner };
            let link = (
                owner.clone(),
                target.clone(),
                canonical(&edge["payload"], false)?,
            );
            if !seen_links.insert(link) {
                continue;
            }
            let mut candidate = packet.clone();
            let next_index = if let Some(index) = admitted.get(next) {
                *index
            } else {
                if admitted.len() >= MAX_NODES {
                    packet["truncated"] = j!(true);
                    continue;
                }
                if !inspected.contains(next) {
                    if inspected.len() >= MAX_INSPECTED {
                        packet["truncated"] = j!(true);
                        continue;
                    }
                    inspected.insert(next.clone());
                    match node(k, next, &source_reader, &mut source_cache) {
                        Ok(value) if tokens(&canonical(&value, false)?) <= budget => {
                            eligible.insert(next.clone(), value);
                        }
                        Ok(_) => packet["truncated"] = j!(true),
                        Err(_) => packet["omitted"] = j!(true),
                    }
                }
                let Some(value) = eligible.get(next) else {
                    continue;
                };
                candidate["nodes"]
                    .as_array_mut()
                    .unwrap()
                    .push(value.clone());
                admitted.len()
            };
            let current_index = admitted[&current];
            let (from, to) = if owner == current {
                (current_index, next_index)
            } else {
                (next_index, current_index)
            };
            candidate["edges"].as_array_mut().unwrap().push(j!({"from":from,"to":to,"relation":relation,"evidence":edge["payload"]["evidence"]}));
            if tokens(&canonical(&candidate, false)?) + 8 > budget {
                packet["truncated"] = j!(true);
                continue;
            }
            packet = candidate;
            if !admitted.contains_key(next) {
                admitted.insert(next.clone(), next_index);
                eligible.remove(next);
                frontier.push_back((next.clone(), level + 1));
            }
        }
        if scanned >= MAX_EDGES {
            packet["truncated"] = j!(true);
            break;
        }
    }
    ensure(
        tokens(&canonical(&packet, false)?) <= budget,
        "graph packet budget",
    )?;
    Ok(packet)
}
