"""Summarize tool-driven blind trials without treating action counts as quality gold."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
from statistics import median
import unicodedata

from .supervisor import LEVELS, current_review


TOOLS = frozenset({"focus_at", "zoom", "orbit", "move", "undo_view"})
COMPLETIONS = frozenset({"observed", "guessed", "unsolved"})


def discover_trace_paths(location: Path) -> list[Path]:
    """Read exported manual traces and nested completed blind-run outputs."""
    if location.is_file():
        return [location]
    if not location.is_dir():
        raise FileNotFoundError(f"Blind trace location does not exist: {location}")
    manual = {path for path in location.glob("*.json")
              if path.name not in {"summary.json", "partial-trace.json"}}
    completed = set(location.rglob("blind-trace.json"))
    return sorted(manual | completed)


def _valid_trace(trace: dict) -> bool:
    actions = trace.get("actions")
    if (trace.get("schema_version") != "sceneqa_blind_trial_v1" or
            trace.get("render_protocol") != "modelviewer_perspective_v1" or
            trace.get("level") not in LEVELS or not isinstance(actions, list) or
            trace.get("successful_action_count") != len(actions) or
            trace.get("completion_status") not in COMPLETIONS or
            (trace.get("completion_status") != "unsolved" and not str(trace.get("answer", "")).strip())):
        return False
    observation = "frame_0001"
    for index, action in enumerate(actions, 2):
        after = f"frame_{index:04d}"
        if (not isinstance(action, dict) or action.get("tool") not in TOOLS or
                not isinstance(action.get("args"), dict) or
                action.get("before_observation_id") != observation or
                action.get("after_observation_id") != after):
            return False
        observation = after
    return trace.get("final_observation_id") == observation


def _bucket(traces: list[dict]) -> dict:
    statuses = Counter(t["completion_status"] for t in traces)
    observed_actions = [len(t["actions"]) for t in traces if t["completion_status"] == "observed"]
    tools = Counter(a["tool"] for t in traces for a in t["actions"])
    sorted_actions = sorted(observed_actions)
    p90 = (sorted_actions[(9 * len(sorted_actions) + 9) // 10 - 1]
           if sorted_actions else None)
    return {"trials": len(traces), "unique_assets": len({t["asset_sha256"] for t in traces}),
            "observed": statuses["observed"], "guessed": statuses["guessed"],
            "unsolved": statuses["unsolved"],
            "median_actions_observed": median(observed_actions) if observed_actions else None,
            "p90_actions_observed": p90,
            "zero_action_observed": sum(n == 0 for n in observed_actions),
            "tool_calls": dict(sorted(tools.items()))}


def summarize_traces(paths: list[Path], runs_root: Path) -> dict:
    root = runs_root.resolve()
    manifests = {}
    for path in root.rglob("output/validation.json"):
        validation = json.loads(path.read_text())
        work = path.parent.parent
        for result in validation["results"]:
            key = (validation.get("scene_id") or work.name, result["level"])
            manifests.setdefault(key, []).append((work, result, validation))
    valid = []
    rejected = {"invalid": [], "stale": []}
    challenges = []
    for path in paths:
        try:
            trace = json.loads(path.read_text())
        except (OSError, ValueError):
            rejected["invalid"].append(str(path))
            continue
        if not isinstance(trace, dict) or not _valid_trace(trace):
            rejected["invalid"].append(str(path))
            continue
        candidates = manifests.get((trace.get("scene_id"), trace["level"]), [])
        current = False
        matched = None
        for work, result, validation in candidates:
            asset = work / "output" / trace["level"] / "scene.glb"
            if (result["question"] == trace.get("question") and
                    result["sha256"] == trace.get("asset_sha256") and
                    result["start_camera"] == trace.get("start_camera_blender") and
                    asset.is_file() and hashlib.sha256(asset.read_bytes()).hexdigest() == result["sha256"]):
                current = True
                matched = (work, result, validation)
                break
        if current:
            valid.append(trace)
            work, result, validation = matched
            supervisor = current_review(work, validation)
            verdict = ((supervisor or {}).get("tiers", {}).get(trace["level"], {}).get("verdict"))
            flags = []
            if trace["completion_status"] == "unsolved":
                flags.append("supervisor_pass_blind_unsolved" if verdict == "pass"
                             else "blind_unsolved_needs_review")
            elif trace["completion_status"] == "guessed":
                flags.append("supervisor_pass_blind_guessed" if verdict == "pass"
                             else "blind_guessed_needs_review")
            elif not trace["actions"]:
                flags.append("supervisor_pass_zero_action_answer" if verdict == "pass"
                             else "blind_zero_action_answer")
            if trace["completion_status"] == "observed":
                independent = unicodedata.normalize("NFKC", str(trace["answer"])).strip().casefold()
                builder = unicodedata.normalize("NFKC", str(result["answer"]["value"])).strip().casefold()
                if independent != builder:
                    flags.append("blind_builder_answer_mismatch")
            if flags:
                challenges.append({"trace": str(path), "scene_id": trace["scene_id"],
                                   "work_rel": str(work.relative_to(root)),
                                   "level": trace["level"], "supervisor_verdict": verdict,
                                   "blind_completion": trace["completion_status"],
                                   "successful_actions": len(trace["actions"]), "flags": flags})
        else:
            rejected["stale"].append(str(path))
    return {"render_protocol": "modelviewer_perspective_v1",
            "note": "Tool counts describe navigation; observed status and answers are self-reported, not correctness gold.",
            "input_traces": len(paths), "valid_traces": len(valid), "rejected": rejected,
            "challenge_flags": challenges,
            "challenge_counts": dict(sorted(Counter(flag for row in challenges
                                                     for flag in row["flags"]).items())),
            "overall": _bucket(valid),
            "by_level": {level: _bucket([t for t in valid if t["level"] == level])
                         for level in LEVELS}}
