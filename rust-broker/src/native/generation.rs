//! Eligibility is independent of source access, extraction and durable publication.
use super::knowledge::keys;
use crate::Session;
use serde_json::{Value, json};

pub const MIN_IDLE_SECONDS: u64 = 24 * 60 * 60;
pub const MIN_REMAINING_PERCENT: f64 = 25.0;
pub const MAX_OBSERVATION_AGE_SECONDS: f64 = 60.0;

pub struct Decision {
    pub reason: &'static str,
    pub remaining_percent: Option<f64>,
}

impl Decision {
    pub fn eligible(&self) -> bool {
        self.reason == "eligible"
    }

    pub fn receipt(&self) -> Value {
        json!({"generated":false,"reason":self.reason,
            "min_rate_limit_remaining_percent":MIN_REMAINING_PERCENT as u64,
            "remaining_percent":self.remaining_percent})
    }
}

pub fn evaluate(session: &Session, chat: &Value, quota: &Value, clock: f64) -> Decision {
    let denied = |reason| Decision {
        reason,
        remaining_percent: None,
    };
    if !session.generate_memories {
        return denied("chat_generation_disabled");
    }
    if !clock.is_finite()
        || keys(
            chat,
            &[
                "observed_at",
                "last_activity_at",
                "active",
                "external_context",
            ],
            &[],
        )
        .is_err()
    {
        return denied("chat_state_unavailable");
    }
    let (Some(observed), Some(activity), Some(active), Some(external)) = (
        chat["observed_at"].as_f64(),
        chat["last_activity_at"].as_f64(),
        chat["active"].as_bool(),
        chat["external_context"].as_bool(),
    ) else {
        return denied("chat_state_unavailable");
    };
    if !(0.0..=MAX_OBSERVATION_AGE_SECONDS).contains(&(clock - observed)) {
        return denied("chat_state_stale");
    }
    if activity > observed {
        return denied("chat_state_unavailable");
    }
    if active {
        return denied("chat_active");
    }
    if external && session.disable_on_external_context {
        return denied("external_context_excluded");
    }
    if clock - activity < MIN_IDLE_SECONDS as f64 {
        return denied("chat_not_idle_long_enough");
    }
    if keys(quota, &["observed_at", "remaining_percent_by_window"], &[]).is_err() {
        return denied("quota_unavailable");
    }
    let (Some(observed), Some(windows)) = (
        quota["observed_at"].as_f64(),
        quota["remaining_percent_by_window"].as_object(),
    ) else {
        return denied("quota_unavailable");
    };
    if windows.is_empty()
        || windows.iter().any(|(name, value)| {
            name.is_empty() || value.as_f64().is_none_or(|n| !(0.0..=100.0).contains(&n))
        })
    {
        return denied("quota_unavailable");
    }
    if !(0.0..=MAX_OBSERVATION_AGE_SECONDS).contains(&(clock - observed)) {
        return denied("quota_stale");
    }
    let remaining = windows
        .values()
        .filter_map(Value::as_f64)
        .fold(100.0, f64::min);
    Decision {
        reason: if remaining < MIN_REMAINING_PERCENT {
            "quota_below_threshold"
        } else {
            "eligible"
        },
        remaining_percent: Some(remaining),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn session() -> Session {
        serde_json::from_value(json!({"id":"synthetic","scope":"synthetic:generation",
            "source_root":"not-read-by-eligibility"}))
        .unwrap()
    }

    #[test]
    fn idle_and_every_quota_boundary_are_inclusive() {
        let s = session();
        let mut chat = json!({"observed_at":100000,"last_activity_at":13600,
            "active":false,"external_context":false});
        let mut quota = json!({"observed_at":100000,
            "remaining_percent_by_window":{"short":25,"weekly":25}});
        assert!(evaluate(&s, &chat, &quota, 100000.0).eligible());
        assert_eq!(
            evaluate(&s, &chat, &quota, 100000.0).receipt()["generated"],
            false
        );
        chat["last_activity_at"] = json!(13600.01);
        assert_eq!(
            evaluate(&s, &chat, &quota, 100000.0).reason,
            "chat_not_idle_long_enough"
        );
        chat["last_activity_at"] = json!(13600);
        quota["remaining_percent_by_window"]["weekly"] = json!(24.99);
        assert_eq!(
            evaluate(&s, &chat, &quota, 100000.0).reason,
            "quota_below_threshold"
        );
    }

    #[test]
    fn eligibility_never_substitutes_recall_permission_or_unknown_telemetry() {
        let mut s = session();
        s.use_memories = false;
        let mut chat = json!({"observed_at":100000,"last_activity_at":13600,
            "active":false,"external_context":false});
        let mut quota = json!({"observed_at":100000,"remaining_percent_by_window":{"short":90}});
        assert!(evaluate(&s, &chat, &quota, 100000.0).eligible());
        assert_eq!(
            evaluate(&s, &chat, &Value::Null, 100000.0).reason,
            "quota_unavailable"
        );
        quota["observed_at"] = json!(99939);
        assert_eq!(evaluate(&s, &chat, &quota, 100000.0).reason, "quota_stale");
        chat["active"] = json!(true);
        assert_eq!(evaluate(&s, &chat, &quota, 100000.0).reason, "chat_active");
        s.generate_memories = false;
        assert_eq!(
            evaluate(&s, &chat, &quota, 100000.0).reason,
            "chat_generation_disabled"
        );
    }
}
