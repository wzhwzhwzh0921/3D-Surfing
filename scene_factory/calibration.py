"""Human-label export and conservative calibration of visual supervisor decisions."""
from __future__ import annotations

import csv
import hashlib
import json
from math import sqrt
from pathlib import Path

from .supervisor import LEVELS, VIEWS, current_review

FIELDS = ("work_rel", "scene_id", "level", "question_type", "question",
          "model_answer_json", "asset_sha256", "evidence_sha256",
          "human_start_leak", "human_verdict", "human_answer",
          "human_answer_correct", "human_note")
REQUIRED_FIELDS = set(FIELDS) - {"human_answer", "human_start_leak"}


def tier_evidence_hash(work: Path, level: str) -> str:
    """Include missing image slots, so adding a view invalidates old labels."""
    digest = hashlib.sha256()
    for view in VIEWS:
        path = work / "output" / level / f"{view}.png"
        digest.update(view.encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b"MISSING")
    return digest.hexdigest()


def make_label_template(runs_root: Path, output: Path, *, overwrite: bool = False) -> dict:
    if output.exists() and not overwrite:
        raise FileExistsError(f"Human label file already exists: {output}")
    root = runs_root.resolve()
    rows = []
    for path in sorted(root.rglob("output/validation.json")):
        work = path.parent.parent
        if any(part.startswith(".") for part in work.relative_to(root).parts):
            continue
        validation = json.loads(path.read_text())
        for result in validation["results"]:
            level = result["level"]
            asset = work / "output" / level / "scene.glb"
            if not asset.is_file():
                continue
            rows.append({"work_rel": str(work.relative_to(root)),
                         "scene_id": validation.get("scene_id") or work.name,
                         "level": level,
                         "question_type": result.get("question_type") or "unknown",
                         "question": result["question"],
                         "model_answer_json": json.dumps(result["answer"]["value"], ensure_ascii=False),
                         "asset_sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                         "evidence_sha256": tier_evidence_hash(work, level),
                         "human_start_leak": "", "human_verdict": "", "human_answer": "",
                         "human_answer_correct": "", "human_note": ""})
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return {"output": str(output), "scenes": len({row["work_rel"] for row in rows}),
            "questions": len(rows), "labeled": 0}


def _wilson_upper(errors: int, total: int) -> float | None:
    if not total:
        return None
    z = 1.96
    p = errors / total
    return (p + z * z / (2 * total) + z * sqrt(p * (1 - p) / total + z * z / (4 * total * total))) / (1 + z * z / total)


def _passes_needed_if_no_more_errors(errors: int, target_upper: float) -> int:
    """Optimistic sample floor, never a predicted number of future passes."""
    if not 0 < target_upper < 1:
        raise ValueError("target_upper must be between zero and one")
    low, high = max(errors, 1), max(errors, 1)
    while _wilson_upper(errors, high) > target_upper:
        high *= 2
    while low < high:
        mid = (low + high) // 2
        if _wilson_upper(errors, mid) <= target_upper:
            high = mid
        else:
            low = mid + 1
    return low


def evaluate_labels(labels: Path, runs_root: Path) -> dict:
    root = runs_root.resolve()
    with labels.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not REQUIRED_FIELDS <= set(reader.fieldnames or []):
            raise ValueError("Label CSV is missing required columns")
        rows = list(reader)
    counts = {"rows": len(rows), "unlabeled": 0, "stale": 0,
              "human_uncertain": 0, "missing_supervisor": 0, "legacy_supervisor": 0,
              "invalid_supervisor": 0, "human_inconsistent": 0,
              "start_leak_yes": 0,
              "gold_binary": 0,
              "supervisor_pass": 0, "supervisor_review": 0, "supervisor_revise": 0,
              "false_accept": 0, "safe_accept": 0, "caught_or_abstained": 0}
    seen = set()
    scene_cache = {}
    pass_scenes = set()
    false_accept_scenes = set()
    by_level = {level: {"gold_binary": 0, "false_accept": 0,
                        "supervisor_pass": 0, "start_leak_yes": 0}
                for level in LEVELS}
    for row in rows:
        key = (row["work_rel"], row["level"])
        if key in seen:
            raise ValueError(f"Duplicate human label row: {key}")
        seen.add(key)
        if row["level"] not in LEVELS:
            raise ValueError(f"Unknown level: {row['level']}")
        verdict = row["human_verdict"].strip().lower()
        correct = row["human_answer_correct"].strip().lower()
        start_leak = (row.get("human_start_leak") or "").strip().lower()
        if (verdict not in ("", "pass", "review", "fail") or
                correct not in ("", "yes", "no", "unknown") or
                start_leak not in ("", "yes", "no", "unknown")):
            raise ValueError(f"Invalid human label: {key}")
        work = (root / row["work_rel"]).resolve()
        if not work.is_relative_to(root):
            raise ValueError(f"Work path escapes runs root: {row['work_rel']}")
        if work not in scene_cache:
            try:
                validation = json.loads((work / "output/validation.json").read_text())
                results = {result["level"]: result for result in validation["results"]}
                fingerprints = {level: (
                    hashlib.sha256((work / "output" / level / "scene.glb").read_bytes()).hexdigest(),
                    tier_evidence_hash(work, level)) for level in results}
                scene_cache[work] = (results, fingerprints, current_review(work, validation))
            except (FileNotFoundError, KeyError, ValueError):
                scene_cache[work] = None
        snapshot = scene_cache[work]
        if snapshot is None or row["level"] not in snapshot[0]:
            current = False
        else:
            result = snapshot[0][row["level"]]
            asset_hash, evidence_hash = snapshot[1][row["level"]]
            current = (result["question"] == row["question"] and
                       json.dumps(result["answer"]["value"], ensure_ascii=False) == row["model_answer_json"] and
                       asset_hash == row["asset_sha256"] and
                       evidence_hash == row["evidence_sha256"])
        if not current:
            counts["stale"] += 1
            continue
        if not verdict:
            counts["unlabeled"] += 1
            continue
        if start_leak == "yes":
            counts["start_leak_yes"] += 1
            by_level[row["level"]]["start_leak_yes"] += 1
        if start_leak == "yes" and verdict == "pass":
            counts["human_inconsistent"] += 1
            continue
        gold_bad = verdict == "fail" or correct == "no" or start_leak == "yes"
        gold_good = verdict == "pass" and correct == "yes"
        if not gold_bad and not gold_good:
            counts["human_uncertain"] += 1
            continue
        report = snapshot[2]
        if report is None:
            counts["missing_supervisor"] += 1
            continue
        if report.get("schema_version") != "sceneqa_supervisor_v2":
            counts["legacy_supervisor"] += 1
            continue
        judgment = (report.get("tiers", {}).get(row["level"], {}).get("verdict"))
        if judgment not in ("pass", "review", "revise"):
            counts["invalid_supervisor"] += 1
            continue
        counts[f"supervisor_{judgment}"] += 1
        counts["gold_binary"] += 1
        by_level[row["level"]]["gold_binary"] += 1
        if judgment == "pass":
            pass_scenes.add(work)
            by_level[row["level"]]["supervisor_pass"] += 1
            if gold_bad:
                counts["false_accept"] += 1
                false_accept_scenes.add(work)
                by_level[row["level"]]["false_accept"] += 1
            else:
                counts["safe_accept"] += 1
        elif gold_bad:
            counts["caught_or_abstained"] += 1
    counts["false_accept_rate_among_pass"] = (
        counts["false_accept"] / counts["supervisor_pass"] if counts["supervisor_pass"] else None)
    counts["false_accept_wilson95_upper"] = _wilson_upper(
        counts["false_accept"], counts["supervisor_pass"])
    # Three questions from one scene share source quality and generation errors.
    # Treating them as three independent draws would overstate precision.
    scene_total = len(pass_scenes)
    scene_errors = len(false_accept_scenes)
    target = 0.10
    needed = _passes_needed_if_no_more_errors(scene_errors, target)
    counts["scene_cluster_audit"] = {
        "scenes_with_supervisor_pass_and_gold": scene_total,
        "scenes_with_any_false_accept": scene_errors,
        "false_accept_wilson95_upper": _wilson_upper(scene_errors, scene_total),
        "pilot_target_upper": target,
        "minimum_scenes_if_no_more_errors": needed,
        "additional_scenes_if_no_more_errors": max(0, needed - scene_total),
        "pilot_target_met": scene_total >= needed,
    }
    # Every scene contributes at most one question to each difficulty. The
    # pooled scene bound can look good while one tier has too few judged passes.
    for level in LEVELS:
        row = by_level[level]
        level_total = row["supervisor_pass"]
        level_errors = row["false_accept"]
        level_needed = _passes_needed_if_no_more_errors(level_errors, target)
        row["audit_quota"] = {
            "independent_scenes_with_pass_and_gold": level_total,
            "false_accept_wilson95_upper": _wilson_upper(level_errors, level_total),
            "pilot_target_upper": target,
            "minimum_scenes_if_no_more_errors": level_needed,
            "additional_scenes_if_no_more_errors": max(0, level_needed - level_total),
            "pilot_target_met": level_total >= level_needed,
        }
    counts["scene_cluster_audit"]["all_levels_target_met"] = all(
        by_level[level]["audit_quota"]["pilot_target_met"] for level in LEVELS)
    counts["by_level"] = by_level
    return counts
