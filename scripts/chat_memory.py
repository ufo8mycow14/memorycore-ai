"""Host-selected chat controls; no native capture or account configuration."""
from dataclasses import dataclass
import math

DEFAULT_MIN_IDLE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class ChatMemoryPolicy:
    use_memories: bool = True
    generate_memories: bool = True
    disable_on_external_context: bool = False
    min_idle_seconds: int = DEFAULT_MIN_IDLE_SECONDS

    def __post_init__(self):
        if any(type(v) is not bool for v in (self.use_memories, self.generate_memories,
                                            self.disable_on_external_context)):
            raise ValueError("chat controls must be boolean")
        if type(self.min_idle_seconds) is not int or self.min_idle_seconds < 3600:
            raise ValueError("idle interval must be at least one hour")

    def generation_reason(self, observation, now):
        if not self.generate_memories:
            return "chat_generation_disabled"
        if not isinstance(observation, dict) or set(observation) != {
                "observed_at", "last_activity_at", "active", "external_context"}:
            return "chat_state_unavailable"
        for key in ("observed_at", "last_activity_at"):
            v = observation[key]
            if type(v) not in (int, float) or not math.isfinite(v):
                return "chat_state_unavailable"
        if type(now) not in (int, float) or not math.isfinite(now):
            return "chat_state_unavailable"
        if any(type(observation[k]) is not bool for k in ("active", "external_context")):
            return "chat_state_unavailable"
        if not 0 <= now - observation["observed_at"] <= 60:
            return "chat_state_stale"
        if observation["last_activity_at"] > observation["observed_at"]:
            return "chat_state_unavailable"
        if observation["active"]:
            return "chat_active"
        if self.disable_on_external_context and observation["external_context"]:
            return "external_context_excluded"
        if now - observation["last_activity_at"] < self.min_idle_seconds:
            return "chat_not_idle_long_enough"
        return None

    def allow_tool(self, name):
        if name in {"memory_propose", "memory_accept"}:
            return self.generate_memories
        # Deletion remains available even with recall disabled.
        if name in {"memory_reject", "memory_forget", "memory_review_forget"}:
            return True
        return self.use_memories
