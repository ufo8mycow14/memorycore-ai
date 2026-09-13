//! Synthetic source-read substep timings; never opens user source data.
use memorycore_ai_broker::{
    Session,
    native::{
        self,
        database::Database,
        knowledge::{self, Knowledge},
    },
};
use std::{collections::BTreeMap, io::Read, time::Instant};

fn main() -> native::Result<()> {
    let root = std::env::temp_dir().join(format!("brain-io-profile-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&root)?;
    let result = profile(&root);
    // Only remove the exact synthetic file and empty directory owned by this run.
    let cleanup =
        std::fs::remove_file(root.join("synthetic.md")).and_then(|()| std::fs::remove_dir(&root));
    result?;
    cleanup?;
    Ok(())
}

fn profile(root: &std::path::Path) -> native::Result<()> {
    let payload = b"Synthetic project uses green tiles and nightly local backups.\n";
    std::fs::write(root.join("synthetic.md"), payload)?;
    let mut samples: BTreeMap<&str, Vec<f64>> = BTreeMap::new();
    macro_rules! timed {
        ($name:literal, $expr:expr) => {{
            let start = Instant::now();
            let value = $expr;
            samples
                .entry($name)
                .or_default()
                .push(start.elapsed().as_secs_f64() * 1e6);
            value
        }};
    }
    let session: Session = serde_json::from_value(serde_json::json!({
        "id":"synthetic", "scope":"synthetic", "source_root":root.to_str().unwrap(), "allow_admin":true
    }))?;
    let db = Database::initialize(rusqlite::Connection::open_in_memory()?)?;
    Knowledge::initialize(&db)?;
    let k = Knowledge::new(&db, &session)?;
    for n in 0..1000 {
        let fresh_name = format!("fresh-{n}.md");
        let fresh_path = root.join(&fresh_name);
        std::fs::write(&fresh_path, payload)?;
        let first_read = timed!(
            "first_source_read",
            knowledge::source_read(&session, &fresh_name)
        );
        std::fs::remove_file(fresh_path)?;
        assert_eq!(first_read?, payload);
        let canonical = timed!("root_canonicalise", std::fs::canonicalize(root)?);
        assert!(timed!("root_is_dir", canonical.is_dir()));
        let target = canonical.join("synthetic.md");
        let metadata = timed!("component_metadata", std::fs::symlink_metadata(&target)?);
        assert!(!metadata.file_type().is_symlink());
        let resolved = timed!("target_canonicalise", std::fs::canonicalize(&target)?);
        assert!(resolved.starts_with(&canonical));
        assert!(timed!("target_is_file", resolved.is_file()));
        let directory = timed!(
            "capability_directory_open",
            cap_std::fs::Dir::open_ambient_dir(&canonical, cap_std::ambient_authority())?
        );
        let file = timed!("capability_file_open", directory.open("synthetic.md")?);
        assert!(timed!("opened_file_metadata", file.metadata()?.is_file()));
        let mut raw = Vec::new();
        timed!("bounded_read", file.take(1048577).read_to_end(&mut raw)?);
        assert_eq!(raw, payload);
        let raw = timed!(
            "full_source_read",
            knowledge::source_read(&session, "synthetic.md")?
        );
        let binding = knowledge::binding("synthetic.md", &raw);
        let memory = db.remember(&session.scope, &serde_json::json!({
            "type":"semantic", "subject":format!("Synthetic item {n}"), "summary":"Uses green tiles",
            "source":"synthetic.md", "source_hash":binding["sha256"]
        }))?;
        let id = memory["memory_id"].as_str().unwrap();
        timed!("verify_memory", db.memory(&session.scope, id, true)?);
        timed!(
            "bind_lifecycle",
            native::chat_lifecycle::bind_source(&k, id, "synthetic.md")?
        );
        timed!("source_items", k.source_items(&[id.to_owned()])?);
        timed!("put_source", k.put("source", binding, Some(id), None)?);
        timed!("queue_bound", native::vectors::queue_bound(&k, id)?);
    }
    let stats: BTreeMap<_, _> = samples
        .into_iter()
        .map(|(name, mut values)| {
            values.sort_by(f64::total_cmp);
            (
                name,
                serde_json::json!({"mean_us": values.iter().sum::<f64>() / values.len() as f64,
            "median_us": values[500], "p99_us": values[989], "samples": values.len()}),
            )
        })
        .collect();
    println!(
        "{}",
        serde_json::json!({"synthetic_only": true, "substeps": stats})
    );
    Ok(())
}
