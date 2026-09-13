"""Local calibration from complete native counters; no model/network calls."""
from .knowledge_layer import digest
from .memory_policy import bounded_integer


def fresh_verification_profile(records):
    """Conservative observed maximum of extra model calls in fresh returns.

    Total per-turn usage must be a cumulative-thread delta. The native last
    counter is only the final model call, never a replacement for total usage.
    This profile applies to analogous fresh retrieval, not unrelated new tasks.
    It estimates future cost; it cannot guarantee future behaviour or savings.
    """
    if not isinstance(records, list) or not records or len(records) > 1000:
        raise ValueError("bounded native observation records are required")
    samples = []
    for record in records:
        native = record["native"]
        if record.get("route", {}).get("action") != "fresh":
            continue
        if record.get("accepted") is not True or native.get("status") != "completed":
            raise ValueError("failed or incomplete observations cannot calibrate an accepted route")
        delta, last = native["usage_delta"], native["usage"]["last"]
        for counts in (delta, last):
            for field in ("inputTokens", "outputTokens", "totalTokens"):
                bounded_integer(counts[field], field, minimum=0, maximum=100_000_000)
            if counts["totalTokens"] != counts["inputTokens"] + counts["outputTokens"]:
                raise ValueError("inconsistent native token counters")
        extra = delta["totalTokens"]-last["totalTokens"]
        if extra < 0:
            raise ValueError("last call exceeds complete per-turn usage")
        samples.append(extra)
    if not samples:
        raise ValueError("no accepted fresh observations")
    return {"format":"fresh-verification-profile/1", "evidence":"calibrated_observation",
            "fresh_verification_tokens":max(samples), "sample_count":len(samples),
            "sample_extra_tokens":samples, "observations_sha256":digest(records),
            "applies_to":"analogous fresh returns with selective source retrieval",
            "future_savings_guaranteed":False}
