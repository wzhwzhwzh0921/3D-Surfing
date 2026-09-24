"""Objective release gates. Semantic completeness still needs independent review."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .download import glb_header
from .schema import answer_from_registry, read_plan


def validate(plan_path: Path, output: Path, *, strict: bool = True) -> dict:
    plan = read_plan(plan_path)
    builds = json.loads((output / "build_report.json").read_text())
    if len(builds) != 3:
        raise ValueError("Missing build reports")
    results = []
    hashes = []
    answers = []
    for variant, build in zip(plan["variants"], builds):
        level = variant["level"]
        if build["level"] != level:
            raise ValueError("Build reports out of order")
        path = output / level / "scene.glb"
        glb_header(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        hashes.append(digest)
        answer = answer_from_registry(variant)
        answers.append(answer["value"])
        declared = {c["id"] for c in variant["copies"]}
        exported = set(build["copy_mesh_names"])
        if declared != exported or build["export_mesh_count"] <= build["original_mesh_count"]:
            raise ValueError(f"{level}: copies missing from exported scene")
        start = build["start_visible_copies"]
        unseen = declared - {cid for cid, visible in start.items() if visible}
        seen_in_probes = {cid for p in build["view_probes"] for cid in p["visible_copy_ids"]}
        coverage = declared <= seen_in_probes
        min_hidden = {"simple": 1, "medium": 2, "hard": 3}[level]
        nontrivial = len(unseen) >= min_hidden
        # Different probes are required to expose all hidden copies. This is an
        # observable proxy, not a claim about 30 navigation actions.
        reachable = [p for p in build["view_probes"] if set(p["visible_copy_ids"]) & unseen]
        gate = coverage and nontrivial and bool(reachable) and not build.get("copy_collisions")
        results.append({"level": level, "scene": str(path), "sha256": digest,
                        "question": variant["question"]["text"], "answer": answer,
                        "start_camera": build["start_camera"], "hidden_copy_ids_at_start": sorted(unseen),
                        "visible_copy_ids_from_probes": sorted(seen_in_probes),
                        "all_copies_discoverable": coverage, "min_hidden_required": min_hidden,
                        "copy_collisions": build.get("copy_collisions", []),
                        "visibility_gate": gate, "size_bytes": path.stat().st_size,
                        "license": plan.get("source", {}).get("license")})
    if len(set(hashes)) != 3:
        raise ValueError("Variant GLBs are identical")
    if len(set(map(str, answers))) != 3:
        raise ValueError("Answers must differ across the three variants")
    report = {"scene_id": plan.get("scene_id"), "status": "candidate" if all(r["visibility_gate"] for r in results) else "failed",
              "results": results,
              "limitations": ["Original entity registry requires independent semantic completeness audit",
                              "Visibility probes do not certify a 30-action human/agent solving difficulty",
                              "Naturalness of placements requires visual review"]}
    (output / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if strict and report["status"] != "candidate":
        raise ValueError("Visibility/difficulty gates failed; inspect validation.json")
    return report
