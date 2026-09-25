"""Shared geometric lower bounds for local scene occlusion."""


def local_occlusion_passes(evidence: dict, *, candidate_probe: bool) -> bool:
    common = (evidence["samples"] >= 10 and
              evidence["mean_blocker_gap"] is not None and
              evidence["mean_blocker_gap"] <= evidence["max_allowed_gap"] and
              evidence["blocker_width_ratio"] <= 0.45)
    if not common:
        return False
    if not candidate_probe:
        return evidence["blocked_fraction"] >= 0.35
    first_hits = sum(evidence.get("first_hit_objects", {}).values())
    return (evidence["blocked_fraction"] >= 0.20 and first_hits > 0 and
            evidence["blocked_samples"] / first_hits >= 0.5)
