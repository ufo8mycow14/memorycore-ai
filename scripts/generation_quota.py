"""Trusted-host quota gate for optional background proposal generation."""
import math
import time
from .chat_memory import ChatMemoryPolicy

MIN_RATE_LIMIT_REMAINING_PERCENT = 25
MAX_OBSERVATION_AGE_SECONDS = 60


def generation_decision(quota, *, now=None, chat=None, policy=None):
    """Check host telemetry without reading a source or running extraction."""
    now = time.time() if now is None else now
    decision = {"generated": False, "min_rate_limit_remaining_percent":
                MIN_RATE_LIMIT_REMAINING_PERCENT, "remaining_percent": None}
    policy = ChatMemoryPolicy() if policy is None else policy
    if not isinstance(policy, ChatMemoryPolicy):
        raise ValueError("host chat policy required")
    reason = policy.generation_reason(chat, now)
    if reason:
        return dict(decision, reason=reason)
    def number(value):
        return type(value) in (int, float) and math.isfinite(value)

    if not isinstance(quota, dict) or set(quota) != {"observed_at", "remaining_percent_by_window"}:
        return dict(decision, reason="quota_unavailable")
    observed = quota["observed_at"]
    windows = quota["remaining_percent_by_window"]
    if (not number(now) or not number(observed) or not isinstance(windows, dict)
            or not windows or any(not isinstance(k, str) or not k for k in windows)
            or any(not number(v) or not 0 <= v <= 100 for v in windows.values())):
        return dict(decision, reason="quota_unavailable")
    if not 0 <= now - observed <= MAX_OBSERVATION_AGE_SECONDS:
        return dict(decision, reason="quota_stale")
    decision["remaining_percent"] = min(windows.values())
    if decision["remaining_percent"] < MIN_RATE_LIMIT_REMAINING_PERCENT:
        return dict(decision, reason="quota_below_threshold")
    return dict(decision, reason="eligible")


def background_propose(knowledge, path, quota, *, now=None, chat=None, policy=None):
    """Generate only after fresh host-owned eligibility checks; manual saves bypass this gate."""
    decision = generation_decision(quota, now=now, chat=chat, policy=policy)
    if decision["reason"] != "eligible":
        return decision
    return dict(decision, generated=True, proposal=knowledge.propose(path))
