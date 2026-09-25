"""Objective release gates. Semantic completeness still needs independent review."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .download import glb_header
from .occlusion_policy import local_occlusion_passes
from .schema import MODERN, answer_from_model, answer_from_registry, read_plan


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
        answer = (answer_from_model(variant) if plan["schema_version"] == "1.3"
                  else answer_from_registry(variant))
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
        answer_copies = declared & set(variant["question"].get("evidence_ids", []))
        exported = set(build["copy_mesh_names"])
        if declared != exported or build["export_mesh_count"] <= build["original_mesh_count"]:
            raise ValueError(f"{level}: copies missing from exported scene")
        start = build["start_visible_copies"]
        unseen = declared - {cid for cid, visible in start.items() if visible}
        seen_in_probes = {cid for p in build["view_probes"] for cid in p["visible_copy_ids"]}
        seen_in_probes |= {p["copy_id"] for p in build.get("local_view_probes", [])}
        coverage = declared <= seen_in_probes
        partial_allowed = plan["schema_version"] == "1.3" and plan.get("workflow_version") == "candidate_probe_v1"
        min_hidden = (0 if partial_allowed else
                      {"simple": 1, "medium": 1 if plan["schema_version"] in ("1.1", *MODERN) else 2,
                       "hard": 1 if plan["schema_version"] in MODERN else
                       2 if plan["schema_version"] == "1.1" else 3}[level])
        # A second angle must expose an answer-bearing copy. This is an
        # observable reachability proxy, not proof that its key surface is seen.
        reachable_ids = answer_copies if partial_allowed else unseen
        start_cam = build["start_camera"]
        reachable = [p for p in build["view_probes"]
                     if set(p["visible_copy_ids"]) & reachable_ids and
                     (not partial_allowed or p["azimuth_deg"] != start_cam["azimuth_deg"] or
                      p["elevation_deg"] != start_cam["elevation_deg"])]
        reachable += [p for p in build.get("local_view_probes", []) if p["copy_id"] in reachable_ids]
        occlusion = build.get("occlusion_evidence", {})
        evidence_views = build.get("evidence_views", {})
        evidence_present = (plan["schema_version"] != "1.3" or
                            all((output / level / f"{name}.png").is_file()
                                for name in (("start", "reveal", "detail", "answer_view")
                                             if plan.get("workflow_version") == "candidate_probe_v1"
                                             else ("start", "reveal", "detail"))))
        answer_view_coverage = evidence_views.get("answer_view_coverage", {})
        answer_view_gate = (plan.get("workflow_version") != "candidate_probe_v1" or
                            answer_view_coverage.get("all_visible") is True)
        occlusion_gate = True
        if level in ("medium", "hard") and plan["schema_version"] in ("1.1", *MODERN):
            # The answer-bearing target must be concealed by a nearby prop;
            # another irrelevant copy does not establish question difficulty.
            new_probe = plan.get("workflow_version") == "candidate_probe_v1"
            passing_occlusion = {cid: evidence for cid, evidence in occlusion.items()
                                 if (not partial_allowed or cid in answer_copies) and
                                 (partial_allowed or cid in unseen) and local_occlusion_passes(
                                     evidence, candidate_probe=new_probe)}
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
        if partial_allowed:
            passing_partial = {cid for cid, evidence in occlusion.items()
                               if cid in answer_copies and local_occlusion_passes(evidence, candidate_probe=True)}
            obscuration_gate = bool((unseen & answer_copies) or passing_partial)
            nontrivial = obscuration_gate
        else:
            passing_partial = set()
            obscuration_gate = len(unseen) >= min_hidden
            nontrivial = obscuration_gate
        gate = (coverage and nontrivial and bool(reachable) and
                not build.get("copy_collisions") and occlusion_gate and evidence_present and
                answer_view_gate)
        results.append({"level": level, "scene": str(path), "sha256": digest,
                        "question": variant["question"]["text"], "answer": answer,
                        "start_camera": build["start_camera"], "hidden_copy_ids_at_start": sorted(unseen),
                        "partly_obscured_copy_ids_at_start": sorted(passing_partial),
                        "obscuration_gate": obscuration_gate,
                        "obscuration_mode": ("whole_copy_hidden" if unseen & answer_copies else
                                             "local_partial_blocker" if passing_partial else "none"),
                        "visible_copy_ids_from_probes": sorted(seen_in_probes),
                        "local_view_probes": build.get("local_view_probes", []),
                        "all_copies_discoverable": coverage, "min_hidden_required": min_hidden,
                        "copy_collisions": build.get("copy_collisions", []),
                        "auto_adjustments": build.get("auto_adjustments", []),
                        "semantic_warnings": semantic_warnings,
                        "occlusion_evidence": occlusion, "occlusion_gate": occlusion_gate,
                        "anchor_query_evidence": build.get("anchor_query_evidence"),
                        "question_type": variant["question"].get("question_type"),
                        "evidence_views": evidence_views, "evidence_present": evidence_present,
                        "answer_view_gate": answer_view_gate,
                        "answer_verified": answer.get("verified", True) and not (
                            plan["schema_version"] in MODERN and level == "hard" and
                            variant["question"]["mode"] in ("count", "compare")),
                        "visibility_gate": gate, "size_bytes": path.stat().st_size,
                        "license": plan.get("source", {}).get("license")})
    if len(set(hashes)) != 3:
        raise ValueError("Variant GLBs are identical")
    if len(set(map(str, answers))) != 3:
        raise ValueError("Answers must differ across the three variants")
    passed = all(r["visibility_gate"] for r in results)
    report = {"scene_id": plan.get("scene_id"),
              "status": "candidate" if passed and (plan["schema_version"] == "1.3" or
                                                    all(r["answer_verified"] for r in results))
                        else "needs_review" if passed else "failed",
              "answer_source": "model_visual" if plan["schema_version"] == "1.3" else "mixed",
              "results": results,
              "limitations": ["Original entity registry requires independent semantic completeness audit",
                              "Visibility probes do not certify a 30-action human/agent solving difficulty",
                              "Naturalness of placements requires visual review"]}
    (output / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if strict and report["status"] == "failed":
        raise ValueError("Visibility/difficulty gates failed; inspect validation.json")
    return report
