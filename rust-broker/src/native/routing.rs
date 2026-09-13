use super::{
    Result, canonical,
    database::{boolean, field, text},
    ensure, json,
    knowledge::{Knowledge, binding, checked, digest, inspect, keys, source_read, tokens},
    policy,
};
use rusqlite::{params, types::Value as Sql};
use serde_json::{Value, json as j};
use std::collections::BTreeSet;

const FIELDS: [&str; 9] = [
    "goal",
    "requirements",
    "decisions",
    "constraints",
    "progress",
    "dependencies",
    "unresolved",
    "unfinished",
    "references",
];
fn number(v: &Value, k: &str, default: i64, min: i64, max: i64) -> Result<i64> {
    let n = match v.get(k) {
        None => default,
        Some(v) => v.as_i64().ok_or("invalid integer")?,
    };
    ensure((min..=max).contains(&n), "integer out of range")?;
    Ok(n)
}
fn placement(v: &Value) -> Result<()> {
    checked(v)?;
    keys(v, &["host", "project_id", "surface"], &[])?;
    ensure(!field(v, "host")?.trim().is_empty(), "host required")?;
    let surface = field(v, "surface")?;
    ensure(
        ["project_chat", "ordinary_chat"].contains(&surface)
            && (v["project_id"].is_null()) == (surface == "ordinary_chat"),
        "placement disagreement",
    )?;
    if !v["project_id"].is_null() {
        ensure(
            !field(v, "project_id")?.trim().is_empty(),
            "project identity required",
        )?;
    }
    Ok(())
}
fn session(v: &Value) -> Result<Value> {
    keys(
        v,
        &["id", "task_key", "placement", "history_tokens"],
        &[
            "status",
            "revision",
            "observed_at",
            "turns_since_transition",
        ],
    )?;
    checked(v)?;
    placement(&v["placement"])?;
    ensure(
        !field(v, "id")?.is_empty() && !field(v, "task_key")?.is_empty(),
        "session identity required",
    )?;
    number(v, "history_tokens", 0, 0, 10_000_000)?;
    number(v, "turns_since_transition", 0, 0, 1_000_000)?;
    let mut v = v.clone();
    for (k, value) in [
        ("status", j!("unknown")),
        ("revision", j!("")),
        ("observed_at", j!(0)),
        ("turns_since_transition", j!(0)),
    ] {
        if v.get(k).is_none() {
            v[k] = value;
        }
    }
    ensure(
        ["idle", "running", "unknown"].contains(&field(&v, "status")?),
        "invalid status",
    )?;
    field(&v, "revision")?;
    v["observed_at"].as_f64().ok_or("invalid observation")?;
    Ok(v)
}
fn boundary(a: &Value, current: &Value, sessions: &[Value]) -> Result<Value> {
    let mut b = if let Some(b) = a.get("boundary") {
        b.clone()
    } else {
        let message = field(a, "message")?;
        policy::check_text(message, 65536)?;
        if regex::Regex::new(
            r"(?i)\b(?:keep (?:this|it) (?:here|in (?:this|the current) chat)|stay in this chat)\b",
        )?
        .is_match(message)
        {
            j!({"task_key":current["task_key"],"keep_here":true})
        } else if let Some(c) = regex::Regex::new(
            r"^\s*(New task|Return to task|Resume task):\s*([a-zA-Z0-9_.-]{1,80})(?:\s|$)",
        )?
        .captures(message)
        {
            let returning = &c[1] != "New task";
            let key = &c[2];
            if returning && !sessions.iter().any(|s| s["task_key"] == key) {
                j!({"task_key":current["task_key"]})
            } else {
                j!({"task_key":key,"meaningful_change":current["task_key"]!=key,"returning":returning,"needs_current_history":false})
            }
        } else {
            j!({"task_key":current["task_key"],"related":true})
        }
    };
    let flags = [
        "meaningful_change",
        "returning",
        "related",
        "tangent",
        "needs_current_history",
        "selective_context_complete",
        "keep_here",
        "explicit_fresh",
    ];
    keys(&b, &["task_key"], &flags)?;
    policy::check_text(field(&b, "task_key")?, 128)?;
    ensure(
        !field(&b, "task_key")?.trim().is_empty(),
        "task key required",
    )?;
    for k in flags {
        let value = boolean(&b, k, k == "needs_current_history")?;
        b[k] = j!(value);
    }
    Ok(b)
}
pub fn plan(a: &Value) -> Result<Value> {
    keys(
        a,
        &["current", "costs"],
        &[
            "boundary",
            "message",
            "sessions",
            "desired_placement",
            "placement_authorised",
        ],
    )?;
    ensure(
        a.get("boundary").is_some() != a.get("message").is_some(),
        "boundary or message required",
    )?;
    let current = session(&a["current"])?;
    let sessions = a
        .get("sessions")
        .cloned()
        .unwrap_or(j!([]))
        .as_array()
        .ok_or("invalid sessions")?
        .iter()
        .map(session)
        .collect::<Result<Vec<_>>>()?;
    let b = boundary(a, &current, &sessions)?;
    let c = &a["costs"];
    let overheads = [
        "classification_tokens",
        "memory_write_tokens",
        "retrieval_tokens",
        "decoding_tokens",
        "retry_rework_tokens",
        "extra_output_tokens",
        "uncertainty_tokens",
    ];
    keys(
        c,
        &["remaining_turns", "fresh_context_tokens"],
        &[
            "classification_tokens",
            "memory_write_tokens",
            "retrieval_tokens",
            "decoding_tokens",
            "retry_rework_tokens",
            "extra_output_tokens",
            "uncertainty_tokens",
            "continue_retrieval_tokens",
            "fresh_verification_tokens",
            "evidence",
        ],
    )?;
    let turns = number(c, "remaining_turns", 1, 1, 100)?;
    let fresh_context = number(c, "fresh_context_tokens", 0, 0, 10_000_000)?;
    let mut overhead = 0;
    for k in overheads {
        overhead += number(c, k, 0, 0, 10_000_000)?;
    }
    let retrieve = number(c, "continue_retrieval_tokens", 0, 0, 10_000_000)?;
    let verify = number(c, "fresh_verification_tokens", 0, 0, 10_000_000)?;
    let evidence = text(c, "evidence", "estimate")?;
    ensure(
        ["estimate", "calibrated_observation"].contains(&evidence),
        "invalid evidence",
    )?;
    let desired = a
        .get("desired_placement")
        .filter(|v| !v.is_null())
        .unwrap_or(&current["placement"]);
    placement(desired)?;
    let mut base = j!({"action":"continue","source":current["id"],"target":current["id"],"placement":current["placement"],"reason":"continuity","cost_evidence":evidence});
    let clock = chrono::Utc::now().timestamp_millis() as f64 / 1000.0;
    let idle = |s: &Value| {
        s["status"] == "idle"
            && s["revision"].as_str().is_some_and(|s| !s.is_empty())
            && s["observed_at"]
                .as_f64()
                .is_some_and(|n| (0.0..=30.0).contains(&(clock - n)))
    };
    let reason = if b["keep_here"] == true {
        Some("user_keep_here")
    } else if desired != &current["placement"] {
        Some("automatic_transition_preserves_exact_placement")
    } else if !idle(&current) {
        Some("active_or_unverified_operations")
    } else if b["tangent"] == true || b["related"] == true {
        Some("related_work_or_tangent")
    } else if b["needs_current_history"] == true || b["selective_context_complete"] != true {
        Some("required_context_not_yet_portable")
    } else if b["meaningful_change"] != true
        && b["returning"] != true
        && b["explicit_fresh"] != true
    {
        Some("no_meaningful_boundary")
    } else if current["turns_since_transition"].as_i64().unwrap() < 3
        && b["returning"] != true
        && b["explicit_fresh"] != true
    {
        Some("fragmentation_cooldown")
    } else {
        None
    };
    if let Some(r) = reason {
        base["reason"] = j!(r);
        return Ok(base);
    }
    let keep = (current["history_tokens"].as_i64().unwrap() + retrieve) * turns;
    let fresh = fresh_context * turns + overhead + verify;
    let mut estimates = j!({"continue":keep,"fresh":fresh});
    let mut candidates = vec![(fresh, 1, String::new())];
    for s in &sessions {
        if s["id"] == current["id"]
            || s["task_key"] != b["task_key"]
            || s["placement"] != *desired
            || !idle(s)
        {
            continue;
        }
        let cost = s["history_tokens"].as_i64().unwrap() * turns + overhead;
        let id = field(s, "id")?.to_owned();
        estimates[format!("resume:{id}")] = j!(cost);
        candidates.push((cost, 0, id));
    }
    candidates.sort();
    let (mut cost, mut kind, mut target) = candidates[0].clone();
    let margin = |n: i64| 256.max((n as f64 * 0.15).round_ties_even() as i64);
    if keep - cost < margin(keep) && b["explicit_fresh"] != true {
        base["reason"] = j!("transition_does_not_repay_total_cost");
        base["estimated_tokens"] = estimates;
        return Ok(base);
    }
    if kind == 1
        && b["explicit_fresh"] != true
        && let Some(r) = candidates.iter().find(|c| c.1 == 0)
        && r.0 - cost < margin(r.0)
    {
        (cost, kind, target) = r.clone();
    }
    if b["explicit_fresh"] == true {
        cost = fresh;
        kind = 1;
        target.clear();
    }
    base["action"] = j!(if kind == 1 { "fresh" } else { "resume" });
    base["target"] = if kind == 1 { Value::Null } else { j!(target) };
    base["placement"] = desired.clone();
    base["task_key"] = b["task_key"].clone();
    base["reason"] = j!(if b["explicit_fresh"] == true {
        "explicit_fresh"
    } else {
        "meaningful_boundary_with_cost_advantage"
    });
    base["estimated_tokens"] = estimates;
    base["estimated_selected_tokens"] = j!(cost);
    for (k, v) in [
        ("source_revision", current["revision"].clone()),
        ("source_observed_at", current["observed_at"].clone()),
        ("source_placement", current["placement"].clone()),
        (
            "placement_authorised",
            j!(a["placement_authorised"] == true),
        ),
        (
            "target_revision",
            sessions
                .iter()
                .find(|s| s["id"] == target)
                .map(|s| s["revision"].clone())
                .unwrap_or(Value::Null),
        ),
    ] {
        base[k] = v;
    }
    Ok(base)
}
pub fn validate_state(v: &Value, complete: bool) -> Result<()> {
    checked(v)?;
    if complete {
        keys(v, &FIELDS, &[])?;
    } else {
        keys(v, &[], &FIELDS)?;
    }
    for (k, v) in v.as_object().unwrap() {
        if k == "goal" {
            ensure(
                v.as_str().is_some_and(|s| !s.trim().is_empty()),
                "goal required",
            )?;
        } else {
            let values = v.as_array().ok_or("invalid state category")?;
            ensure(
                values.len() <= 100
                    && values
                        .iter()
                        .all(|v| v.as_str().is_some_and(|s| !s.trim().is_empty())),
                "complete state facts required",
            )?;
        }
    }
    Ok(())
}
fn selected(v: &Value, task: &Value) -> Result<()> {
    keys(v, &["task_key", "state"], &[])?;
    ensure(&v["task_key"] == task, "unrelated selected state")?;
    validate_state(&v["state"], false)?;
    ensure(
        tokens(&canonical(v, false)?) <= 1400,
        "selected state exceeds budget",
    )
}
pub fn checkpoint(k: &Knowledge, a: &Value) -> Result<Value> {
    keys(
        a,
        &["task_key", "source_session", "state"],
        &["source_paths"],
    )?;
    checked(a)?;
    validate_state(&a["state"], true)?;
    let task = field(a, "task_key")?;
    let source = field(a, "source_session")?;
    let mut bindings = Vec::new();
    if let Some(paths) = a.get("source_paths") {
        for p in paths.as_array().ok_or("invalid paths")? {
            let path = p.as_str().ok_or("invalid path")?;
            bindings.push(binding(path, &source_read(k.session, path)?));
        }
    }
    let package = j!({"format":"session-checkpoint/1","scope":k.session.scope,"task_key":task,"source_session":source,"state":a["state"],"source_bindings":bindings});
    let subject = format!("Session state: {task}");
    let mut previous = Vec::new();
    for row in k.candidates(&BTreeSet::new())? {
        if k.db.verify_memory(&row)?[0] == subject {
            previous.push(hex::encode(row.bytes("memory_id")?));
        }
    }
    ensure(previous.len() <= 1, "ambiguous checkpoint head")?;
    let saved=k.db.remember(&k.session.scope,&j!({"type":"semantic","subject":subject,"summary":a["state"]["goal"],"detail":canonical(&a["state"],false)?,"source":source,"keywords":task,"supersedes":previous.first()}))?;
    let exact=k.db.store_exact(&k.session.scope,&j!({"text":canonical(&package,false)?,"source":source,"media_type":"application/json","user_confirmed":true,"linked_memory_id":saved["memory_id"]}))?;
    Ok(
        j!({"memory_id":saved["memory_id"],"archive_id":exact["archive_id"],"sha256":digest(&package)?}),
    )
}
pub fn recall(k: &Knowledge, a: &Value) -> Result<Value> {
    keys(a, &["receipt"], &["categories", "max_tokens"])?;
    let receipt = &a["receipt"];
    keys(receipt, &["memory_id", "archive_id", "sha256"], &[])?;
    let categories = a.get("categories").cloned().unwrap_or(j!(FIELDS));
    let categories = categories.as_array().ok_or("invalid categories")?;
    let mut seen = BTreeSet::new();
    for c in categories {
        let c = c.as_str().ok_or("invalid category")?;
        ensure(
            FIELDS.contains(&c) && seen.insert(c),
            "unknown or duplicate category",
        )?;
    }
    let (exact, raw) =
        k.db.exact_bytes(&k.session.scope, field(receipt, "archive_id")?)?;
    let p = json(std::str::from_utf8(&raw)?)?;
    ensure(
        p["format"] == "session-checkpoint/1"
            && p["scope"] == k.session.scope
            && digest(&p)? == receipt["sha256"],
        "checkpoint receipt mismatch",
    )?;
    let (head, _) =
        k.db.memory(&k.session.scope, field(receipt, "memory_id")?, true)?;
    ensure(
        exact.bytes("linked_memory_id")? == head.bytes("memory_id")?,
        "checkpoint head mismatch",
    )?;
    let bindings = p["source_bindings"]
        .as_array()
        .ok_or("invalid source bindings")?;
    for b in bindings {
        ensure(
            inspect(k.session, b) == "fresh",
            "checkpoint source changed",
        )?;
    }
    validate_state(&p["state"], true)?;
    let mut state = serde_json::Map::new();
    for c in &seen {
        state.insert(c.to_string(), p["state"][*c].clone());
    }
    let result = j!({"task_key":p["task_key"],"data_only":true,"state":state,"unloaded_categories":FIELDS.into_iter().filter(|k|!seen.contains(k)).collect::<Vec<_>>(),"source_material_still_authoritative":true,"source_status":if bindings.is_empty(){"unverified"}else{"fresh"},"source_bindings":bindings});
    ensure(
        tokens(&canonical(&result, false)?) <= number(a, "max_tokens", 1400, 1, 8000)? as usize,
        "required context exceeds budget",
    )?;
    Ok(result)
}
fn read(k: &Knowledge, id: &str) -> Result<Value> {
    let row =
        k.db.rows(
            "SELECT payload,checksum FROM session_route WHERE scope=? AND message_id=?",
            vec![Sql::Text(k.session.scope.clone()), Sql::Text(id.into())],
        )?
        .into_iter()
        .next()
        .ok_or("route intent not found")?;
    let v = json(row.text("payload")?)?;
    ensure(
        digest(&v)? == row.text("checksum")?
            && v["scope"] == k.session.scope
            && v["message_id"] == id,
        "route integrity failure",
    )?;
    checked(&v)?;
    Ok(v)
}
fn write(k: &Knowledge, v: &Value) -> Result<()> {
    checked(v)?;
    k.db.conn.execute("INSERT INTO session_route VALUES(?,?,?,?) ON CONFLICT(scope,message_id) DO UPDATE SET payload=excluded.payload,checksum=excluded.checksum",params![k.session.scope,field(v,"message_id")?,canonical(v,false)?,digest(v)?])?;
    Ok(())
}
fn prepare(k: &Knowledge, a: &Value) -> Result<Value> {
    keys(
        a,
        &[
            "message_id",
            "message",
            "route",
            "current_task",
            "state",
            "target_packet",
        ],
        &[],
    )?;
    let id = field(a, "message_id")?;
    let message = field(a, "message")?;
    policy::check_text(id, 128)?;
    policy::check_text(message, 65536)?;
    let r = &a["route"];
    checked(r)?;
    ensure(
        !id.is_empty() && !message.is_empty() && ["fresh", "resume"].contains(&field(r, "action")?),
        "transition proposal required",
    )?;
    selected(&a["target_packet"], &r["task_key"])?;
    placement(&r["placement"])?;
    placement(&r["source_placement"])?;
    ensure(
        r["placement"] == r["source_placement"],
        "exact placement required",
    )?;
    ensure(
        !field(r, "source")?.is_empty() && !field(r, "source_revision")?.is_empty(),
        "source revision required",
    )?;
    if r["action"] == "resume" {
        ensure(
            !field(r, "target")?.is_empty()
                && r["target"] != r["source"]
                && !field(r, "target_revision")?.is_empty(),
            "verified distinct target required",
        )?;
    } else {
        ensure(r["target"].is_null(), "fresh must not name existing target")?;
    }
    validate_state(&a["state"], true)?;
    let signature = digest(
        &j!({"message":message,"route":r,"target_packet":a["target_packet"],"current_task":a["current_task"],"state":a["state"]}),
    )?;
    if k.db.conn.query_row(
        "SELECT count(*) FROM session_route WHERE scope=? AND message_id=?",
        params![k.session.scope, id],
        |r| r.get::<_, i64>(0),
    )? > 0
    {
        let item = read(k, id)?;
        ensure(item["signature"] == signature, "message identity collision")?;
        return Ok(item);
    }
    let saved = checkpoint(
        k,
        &j!({"task_key":a["current_task"],"source_session":r["source"],"state":a["state"]}),
    )?;
    let item = j!({"scope":k.session.scope,"message_id":id,"signature":signature,"message":message,"message_sha256":digest(&j!(message))?,"route":r,"checkpoint":saved,"target_packet":a["target_packet"],"state":"PREPARED","attempts":0,"receipt":null});
    write(k, &item)?;
    Ok(item)
}
fn handoff(k: &Knowledge, id: &str) -> Result<Value> {
    let mut item = read(k, id)?;
    if item["state"] == "DELIVERED" {
        return Ok(item["receipt"].clone());
    }
    ensure(
        !["DISPATCHING", "RECONCILE", "CANCELLED"].contains(&field(&item, "state")?),
        "reconcile uncertain or cancelled delivery",
    )?;
    item["state"] = j!("HANDOFF");
    write(k, &item)?;
    Ok(
        j!({"occurred":false,"state":"HANDOFF","target_placement":item["route"]["placement"],"target_session":item["route"]["target"],"checkpoint":item["checkpoint"],"pending_message":item["message"],"selective_context":item["target_packet"],"source_preserved":true,
        "limitation":"Automatic delivery requires verified placement, history isolation and reliable pending-message delivery; no fork or copied-history fallback is allowed.",
        "user_action":"Preserve the current conversation. Use a verified thread/start destination in the exact same project or ordinary-chat surface for fresh routing; resume remains a separate operation. Deliver the exact pending message once."}),
    )
}
fn calibrate(a: &Value) -> Result<Value> {
    keys(a, &["records"], &[])?;
    let records = a["records"].as_array().ok_or("invalid observations")?;
    ensure((1..=1000).contains(&records.len()), "observation limit")?;
    let mut samples = Vec::new();
    for record in records {
        if record["route"]["action"] != "fresh" {
            continue;
        }
        let n = &record["native"];
        ensure(
            record["accepted"] == true && n["status"] == "completed",
            "incomplete observation",
        )?;
        let delta = &n["usage_delta"];
        let last = &n["usage"]["last"];
        for c in [delta, last] {
            for key in ["inputTokens", "outputTokens", "totalTokens"] {
                ensure(c.get(key).is_some(), "missing counter")?;
                number(c, key, 0, 0, 100_000_000)?;
            }
            ensure(
                c["inputTokens"].as_i64().unwrap() + c["outputTokens"].as_i64().unwrap()
                    == c["totalTokens"].as_i64().unwrap(),
                "inconsistent counters",
            )?;
        }
        let extra = delta["totalTokens"].as_i64().unwrap() - last["totalTokens"].as_i64().unwrap();
        ensure(extra >= 0, "incomplete total usage")?;
        samples.push(extra);
    }
    ensure(!samples.is_empty(), "no accepted fresh observations")?;
    Ok(
        j!({"format":"fresh-verification-profile/1","evidence":"calibrated_observation","fresh_verification_tokens":samples.iter().max(),"sample_count":samples.len(),"sample_extra_tokens":samples,"observations_sha256":digest(&a["records"])?,"applies_to":"analogous fresh returns with selective source retrieval","future_savings_guaranteed":false}),
    )
}
pub fn execute(k: &Knowledge, op: &str, a: &Value) -> Result<Value> {
    match op {
        "routing-plan" => plan(a),
        "routing-checkpoint" => checkpoint(k, a),
        "routing-recall" => recall(k, a),
        "routing-prepare" => prepare(k, a),
        "routing-calibrate" => calibrate(a),
        "routing-handoff" => {
            keys(a, &["message_id"], &[])?;
            handoff(k, field(a, "message_id")?)
        }
        "routing-cancel" => {
            keys(a, &["message_id"], &[])?;
            let mut item = read(k, field(a, "message_id")?)?;
            ensure(
                ["PREPARED", "HANDOFF"].contains(&field(&item, "state")?),
                "reconcile before cancellation",
            )?;
            item["state"] = j!("CANCELLED");
            write(k, &item)?;
            Ok(j!({"continue_original":true,"message":item["message"],"source_preserved":true}))
        }
        "routing-export" => {
            keys(a, &[], &[])?;
            let rows = k.db.rows(
                "SELECT message_id FROM session_route WHERE scope=? ORDER BY message_id LIMIT 2001",
                vec![Sql::Text(k.session.scope.clone())],
            )?;
            ensure(rows.len() <= 2000, "routing export limit")?;
            let items = rows
                .iter()
                .map(|r| read(k, r.text("message_id")?))
                .collect::<Result<Vec<_>>>()?;
            let mut body = j!({"format":"session-routing-audit/1","knowledge":k.export(true)?,"intents":items,"automatic_delivery_restore":false});
            body["sha256"] = j!(digest(&body)?);
            Ok(body)
        }
        _ => Err("unsupported route operation".into()),
    }
}
