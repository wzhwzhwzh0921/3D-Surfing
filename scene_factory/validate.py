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
        semantic_warnings = []
        for entity in variant["entities"]:
            kind = str(entity["kind"]).lower().strip()
            names = " ".join(entity["objects"]).lower()
            if kind.isascii() and kind.replace("_", "").isalpha() and kind not in names:
                semantic_warnings.append({"entity_id": entity["id"], "claimed_kind": kind,
                                          "source_meshes": entity["objects"],
                                          "warning": "kind label is not supported by mesh names; visual audit required"})
        declared = {c["id"] for c in variant["copies"]}
        exported = set(build["copy_mesh_names"])
        if declared != exported or build["export_mesh_count"] <= build["original_mesh_count"]:
            raise ValueError(f"{level}: copies missing from exported scene")
        start = build["start_visible_copies"]
        unseen = declared - {cid for cid, visible in start.items() if visible}
        seen_in_probes = {cid for p in build["view_probes"] for cid in p["visible_copy_ids"]}
        seen_in_probes |= {p["copy_id"] for p in build.get("local_view_probes", [])}
        coverage = declared <= seen_in_probes
        min_hidden = {"simple": 1, "medium": 1 if plan["schema_version"] in ("1.1", "1.2") else 2,
                      "hard": 1 if plan["schema_version"] == "1.2" else
                      2 if plan["schema_version"] == "1.1" else 3}[level]
        nontrivial = len(unseen) >= min_hidden
        # Different probes are required to expose all hidden copies. This is an
        # observable proxy, not a claim about 30 navigation actions.
        reachable = [p for p in build["view_probes"] if set(p["visible_copy_ids"]) & unseen]
        reachable += [p for p in build.get("local_view_probes", []) if p["copy_id"] in unseen]
        occlusion = build.get("occlusion_evidence", {})
        occlusion_gate = True
        if level in ("medium", "hard") and plan["schema_version"] in ("1.1", "1.2"):
            # A hidden target must be concealed by a named, nearby scene prop,
            # rather than merely disappearing behind the global perimeter wall.
            passing_occlusion = {cid: evidence for cid, evidence in occlusion.items()
                                 if cid in unseen and evidence["samples"] >= 10 and
                                 evidence["blocked_fraction"] >= 0.35 and
                                 evidence["mean_blocker_gap"] is not None and
                                 evidence["mean_blocker_gap"] <= evidence["max_allowed_gap"] and
                                 evidence["blocker_width_ratio"] <= 0.45}
            if level == "medium":
                occlusion_gate = bool(passing_occlusion)
            elif plan["schema_version"] == "1.1":
                blockers = {name for evidence in passing_occlusion.values()
                            for name in evidence["expected_blocker_objects"]}
                occlusion_gate = len(passing_occlusion) >= 2 and len(blockers) >= 2
                if variant["question"]["mode"] == "identify":
                    occlusion_gate &= variant["question"]["answer_copy_id"] in passing_occlusion
            else:
                query = variant["anchor_query"]
                anchor = build.get("anchor_query_evidence") or {}
                occlusion_gate = (anchor.get("resolved_anchor_object") == query["answer_anchor_object"] and
                                  anchor.get("margin_ratio", 0) >= 0.15 and
                                  query["target_copy_id"] in passing_occlusion)
        gate = coverage and nontrivial and bool(reachable) and not build.get("copy_collisions") and occlusion_gate
        results.append({"level": level, "scene": str(path), "sha256": digest,
                        "question": variant["question"]["text"], "answer": answer,
                        "start_camera": build["start_camera"], "hidden_copy_ids_at_start": sorted(unseen),
                        "visible_copy_ids_from_probes": sorted(seen_in_probes),
                        "local_view_probes": build.get("local_view_probes", []),
                        "all_copies_discoverable": coverage, "min_hidden_required": min_hidden,
                        "copy_collisions": build.get("copy_collisions", []),
                        "auto_adjustments": build.get("auto_adjustments", []),
                        "semantic_warnings": semantic_warnings,
                        "occlusion_evidence": occlusion, "occlusion_gate": occlusion_gate,
                        "anchor_query_evidence": build.get("anchor_query_evidence"),
                        "question_type": variant["question"].get("question_type"),
                        "answer_verified": answer.get("verified", True) and not (
                            plan["schema_version"] == "1.2" and level == "hard" and
                            variant["question"]["mode"] in ("count", "compare")),
                        "visibility_gate": gate, "size_bytes": path.stat().st_size,
                        "license": plan.get("source", {}).get("license")})
    if len(set(hashes)) != 3:
        raise ValueError("Variant GLBs are identical")
    if len(set(map(str, answers))) != 3:
        raise ValueError("Answers must differ across the three variants")
    passed = all(r["visibility_gate"] for r in results)
    report = {"scene_id": plan.get("scene_id"),
              "status": "candidate" if passed and all(r["answer_verified"] for r in results)
                        else "needs_review" if passed else "failed",
              "results": results,
              "limitations": ["Original entity registry requires independent semantic completeness audit",
                              "Visibility probes do not certify a 30-action human/agent solving difficulty",
                              "Naturalness of placements requires visual review"]}
    (output / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if strict and report["status"] == "failed":
        raise ValueError("Visibility/difficulty gates failed; inspect validation.json")
    return report
