"""Strict, inspectable plan contract shared by agent and validator."""
from __future__ import annotations

import json
import hashlib
from copy import deepcopy
from pathlib import Path

from .question_types import QUESTION_TYPES

LEVELS = ("simple", "medium", "hard")
MODERN = ("1.2", "1.3")


def read_candidate_pool(path: str | Path) -> dict:
    """Validate the six inexpensive proposals before rendering any full GLB."""
    pool = json.loads(Path(path).read_text(encoding="utf-8"))
    candidates = pool.get("candidates")
    if pool.get("schema_version") != "candidate_pool_1" or not isinstance(candidates, list) or len(candidates) != 6:
        raise ValueError("Candidate pool needs schema_version candidate_pool_1 and six candidates")
    ids = [c.get("candidate_id") for c in candidates]
    if any(not isinstance(x, str) or not x.replace("_", "").replace("-", "").isalnum()
           for x in ids) or len(set(ids)) != 6:
        raise ValueError("Candidate IDs must be unique letters, digits, _ or -")
    if any(sum(c.get("level") == level for c in candidates) != 2 for level in LEVELS):
        raise ValueError("Candidate pool needs two proposals per difficulty level")
    for level in LEVELS:
        first, second = (deepcopy(c) for c in candidates if c["level"] == level)
        for item in (first, second):
            item.pop("candidate_id", None)
            item.pop("start_camera", None)
        if first == second:
            raise ValueError(f"{level}: two distinct proposals required")
    for c in candidates:
        if not c.get("entities") or not c.get("copies") or not c.get("question", {}).get("text"):
            raise ValueError(f"{c['candidate_id']}: entities, copies and question required")
        landmarks = c.get("start_landmarks")
        if not isinstance(landmarks, list) or not landmarks or any(
            not isinstance(item, dict) or not item.get("label") or
            not isinstance(item.get("objects"), list) or not item["objects"]
            for item in landmarks
        ):
            raise ValueError(f"{c['candidate_id']}: declare visible start_landmarks")
        if c["level"] in ("medium", "hard") and not c.get("occlusion_targets"):
            raise ValueError(f"{c['candidate_id']}: local occlusion required")
        if c["level"] == "hard" and not c.get("anchor_query"):
            raise ValueError(f"{c['candidate_id']}: nearest-anchor query required")
    return pool


def validate_selection(plan: dict, pool: dict, probe_report: list[dict]) -> None:
    """The final plan must use camera-tested proposals, not unprobed edits."""
    if plan.get("workflow_version") != "candidate_probe_v1":
        raise ValueError("Final plan must declare workflow_version candidate_probe_v1")
    by_id = {c["candidate_id"]: c for c in pool["candidates"]}
    reports = {r["candidate_id"]: r for r in probe_report}
    for variant in plan["variants"]:
        cid = variant.get("candidate_id")
        if cid not in by_id or cid not in reports or by_id[cid]["level"] != variant["level"]:
            raise ValueError(f"{variant['level']}: select one probed candidate of the same level")
        if reports[cid].get("probe_protocol_version") != "landmark_surface_v3":
            raise ValueError(f"{cid}: probe report uses an old camera protocol; run probe again")
        digest = hashlib.sha256(json.dumps(by_id[cid], ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
        if reports[cid].get("candidate_sha256") != digest:
            raise ValueError(f"{cid}: candidate changed since probe; run probe again")
        original = deepcopy(by_id[cid])
        selected = deepcopy(variant)
        for value in (original, selected):
            value.pop("candidate_id", None)
            value.pop("start_camera", None)
            value.get("question", {}).pop("declared_answer", None)
        if selected != original:
            raise ValueError(f"{cid}: proposal changed after probe; rerun probe before selecting")
        cam = variant["start_camera"]
        if not any(o["eligible"] and o["azimuth_deg"] == cam["azimuth_deg"] and
                   o["elevation_deg"] == cam["elevation_deg"]
                   for o in reports[cid]["recommended"]):
            raise ValueError(f"{cid}: selected camera did not pass the landmark/occlusion probe")


def select_candidates(pool: dict, probe_report: list[dict], choices: dict) -> dict:
    """Copy selected proposals into a final plan without hand-transcribing fields."""
    candidates = {c["candidate_id"]: c for c in pool["candidates"]}
    reports = {r["candidate_id"]: r for r in probe_report}
    variants = []
    for level in LEVELS:
        cid, view_index = choices[level]
        if cid not in candidates or cid not in reports or candidates[cid]["level"] != level:
            raise ValueError(f"{level}: choose a probed candidate of this level")
        views = reports[cid]["recommended"]
        if not 1 <= view_index <= len(views) or not views[view_index - 1]["eligible"]:
            raise ValueError(f"{cid}: choose an eligible preview index")
        variant = deepcopy(candidates[cid])
        view = views[view_index - 1]
        variant["start_camera"] = {"azimuth_deg": view["azimuth_deg"],
                                   "elevation_deg": view["elevation_deg"]}
        variants.append(variant)
    plan = {"schema_version": "1.3", "workflow_version": "candidate_probe_v1",
            "variants": variants}
    validate_selection(plan, pool, probe_report)
    return plan


def read_plan(path: str | Path) -> dict:
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    if plan.get("schema_version") not in ("1.0", "1.1", *MODERN):
        raise ValueError("schema_version must be 1.0, 1.1, 1.2 or 1.3")
    variants = plan.get("variants")
    if not isinstance(variants, list) or len(variants) != 3:
        raise ValueError("Exactly three variants are required")
    if [v.get("level") for v in variants] != list(LEVELS):
        raise ValueError("Variant order must be simple, medium, hard")
    kinds, questions, question_types = [], [], []
    for v in variants:
        level = v["level"]
        entities = v.get("entities")
        if not isinstance(entities, list) or not entities:
            raise ValueError(f"{level}: declare all relevant original entities")
        copies = v.get("copies")
        if not isinstance(copies, list) or not copies:
            raise ValueError(f"{level}: at least one copy is required")
        ids = [e.get("id") for e in entities]
        if any(not x for x in ids) or len(ids) != len(set(ids)):
            raise ValueError(f"{level}: entity ids missing or duplicate")
        entity_index = {e["id"]: e for e in entities}
        mesh_owners = {}
        for e in entities:
            if not e.get("kind") or not isinstance(e.get("objects"), list) or not e["objects"]:
                raise ValueError(f"{level}: every original entity needs kind and mesh objects")
            for name in e["objects"]:
                if name in mesh_owners:
                    raise ValueError(f"{level}: original mesh {name} assigned to multiple entities")
                mesh_owners[name] = e["id"]
        copy_ids = [c.get("id") for c in copies]
        if any(not x for x in copy_ids) or len(copy_ids) != len(set(copy_ids)) or set(copy_ids) & set(ids):
            raise ValueError(f"{level}: copy ids missing or duplicate")
        for c in copies:
            if c.get("from") not in entity_index:
                raise ValueError(f"{level}: copy source is not declared")
            if not isinstance(c.get("xy"), list) or len(c["xy"]) != 2:
                raise ValueError(f"{level}: copy needs world-space xy")
            if not c.get("support_object"):
                raise ValueError(f"{level}: copy needs named support surface")
        if level in ("medium", "hard") and plan["schema_version"] in ("1.1", *MODERN):
            if level == "medium" and len(copies) > 2:
                raise ValueError("medium: use at most two new targets; difficulty should come from occlusion")
            occlusion_targets = v.get("occlusion_targets")
            if not isinstance(occlusion_targets, list) or not occlusion_targets:
                raise ValueError(f"{level}: declare occlusion targets")
            if level == "hard" and plan["schema_version"] == "1.1" and (len(occlusion_targets) < 2 or len({
                t.get("target_copy_id") for t in occlusion_targets}) < 2):
                raise ValueError("hard: at least two distinct hidden targets are required")
            for target in occlusion_targets:
                if target.get("target_copy_id") not in copy_ids:
                    raise ValueError(f"{level}: occlusion target must be a declared target copy")
                blockers = target.get("blocker_objects")
                if not isinstance(blockers, list) or not blockers or any(
                    not isinstance(name, str) or not name for name in blockers
                ):
                    raise ValueError(f"{level}: list the original mesh objects that act as blockers")
            if level == "hard" and plan["schema_version"] == "1.1" and len({name for t in occlusion_targets
                                        for name in t["blocker_objects"]}) < 2:
                raise ValueError("hard: use at least two distinct local occluders")
        if level == "hard" and plan["schema_version"] in MODERN:
            anchor = v.get("anchor_query", {})
            candidates = anchor.get("candidate_objects")
            if anchor.get("relation") != "nearest" or not isinstance(candidates, list) or len(set(candidates)) < 2:
                raise ValueError("hard: nearest anchor query needs two distinct candidate objects")
            if anchor.get("answer_anchor_object") not in candidates or not anchor.get("reference_object"):
                raise ValueError("hard: anchor query must name its reference and answer anchor")
            if anchor.get("target_copy_id") not in copy_ids or not any(
                t["target_copy_id"] == anchor["target_copy_id"] and
                anchor["answer_anchor_object"] in t["blocker_objects"]
                for t in v["occlusion_targets"]
            ):
                raise ValueError("hard: answer target must be occluded by the resolved anchor")
        q = v.get("question", {})
        if q.get("mode") not in ("count", "compare", "identify", "evidence") or not q.get("text"):
            raise ValueError(f"{level}: question mode and text required")
        if plan["schema_version"] in MODERN:
            question_type = q.get("question_type")
            if question_type not in QUESTION_TYPES:
                raise ValueError(f"{level}: question_type must be one of the ten catalog types")
            if question_type == "count" and q["mode"] not in ("count", "compare"):
                raise ValueError(f"{level}: count type needs count/compare mode")
            if question_type == "identity" and q["mode"] != "identify":
                raise ValueError(f"{level}: identity type needs identify mode")
            if question_type not in ("count", "identity") and q["mode"] != "evidence":
                raise ValueError(f"{level}: this question type needs an evidence draft")
            question_types.append(question_type)
        if q["mode"] == "evidence" or plan["schema_version"] == "1.3":
            if plan["schema_version"] not in MODERN or q.get("declared_answer") in (None, ""):
                raise ValueError(f"{level}: evidence draft needs a declared answer")
            evidence_ids = q.get("evidence_ids")
            if not isinstance(evidence_ids, list) or not evidence_ids or any(
                eid not in set(ids) | set(copy_ids) for eid in evidence_ids
            ):
                raise ValueError(f"{level}: evidence_ids must refer to declared objects or copies")
        if q.get("kind") not in {e["kind"] for e in entities}:
            raise ValueError(f"{level}: target kind absent from entity registry")
        if q["mode"] == "compare" and q.get("other_kind") not in {e["kind"] for e in entities}:
            raise ValueError(f"{level}: comparison kind absent")
        if q["mode"] == "identify":
            answer_copy = next((c for c in copies if c["id"] == q.get("answer_copy_id")), None)
            if answer_copy is None or entity_index[answer_copy["from"]]["kind"] != q["kind"]:
                raise ValueError(f"{level}: identify answer must refer to a copied target of the stated kind")
            if level == "hard" and plan["schema_version"] == "1.1":
                hidden_ids = {t["target_copy_id"] for t in v["occlusion_targets"]}
                hidden_kinds = {entity_index[c["from"]]["kind"] for c in copies if c["id"] in hidden_ids}
                if len(hidden_kinds) < 2:
                    raise ValueError("hard: identify requires a different hidden distractor kind")
            if plan["schema_version"] in ("1.1", *MODERN) and not any(
                t["target_copy_id"] == answer_copy["id"] and
                q.get("anchor_blocker_object") in t["blocker_objects"]
                for t in v.get("occlusion_targets", [])
            ):
                raise ValueError(f"{level}: identify answer must be tied to its named occluding anchor")
        if level == "hard" and plan["schema_version"] == "1.0" and q["mode"] != "compare":
            raise ValueError("hard: use a comparison requiring two groups")
        if level == "hard" and plan["schema_version"] in MODERN and q["mode"] == "identify" and (
            q["answer_copy_id"] != v["anchor_query"]["target_copy_id"]
        ):
            raise ValueError("hard: identify answer must match the resolved anchor target")
        cam = v.get("start_camera", {})
        if not isinstance(cam.get("azimuth_deg"), (int, float)) or not isinstance(cam.get("elevation_deg"), (int, float)):
            raise ValueError(f"{level}: explicit starting camera required")
        if not 5 <= cam["elevation_deg"] <= 80:
            raise ValueError(f"{level}: starting elevation must be 5..80 degrees")
        kinds.append(q["kind"])
        questions.append(q["text"].strip())
    if len(set(questions)) != 3:
        raise ValueError("Three variants must have different question text")
    if plan["schema_version"] in MODERN:
        if len(set(question_types)) != 3:
            raise ValueError("Three variants must use different question types")
    elif len(set(kinds)) != 3:
        raise ValueError("Three variants must target different object kinds")
    return plan


def answer_from_model(variant: dict) -> dict:
    """Record the agent's visual answer without claiming program verification."""
    q = variant["question"]
    return {"value": q["declared_answer"], "source": "model_visual",
            "evidence_ids": q["evidence_ids"], "verified": False}


def answer_from_registry(variant: dict) -> dict:
    """Compute exact answer from declared originals plus copies, never model prose."""
    entities = [dict(e) for e in variant["entities"]]
    by_id = {e["id"]: e for e in entities}
    for c in variant["copies"]:
        original = by_id[c["from"]]
        entities.append({"id": c["id"], "kind": original["kind"],
                         "pose": c.get("pose", original.get("pose")),
                         "region": c.get("region", original.get("region"))})
    q = variant["question"]
    if q["mode"] == "evidence":
        return {"value": q["declared_answer"], "derivation": "model-declared draft; human/typed verification required",
                "evidence_ids": q["evidence_ids"], "verified": False}
    if q["mode"] == "identify":
        chosen = next(e for e in entities if e["id"] == q["answer_copy_id"])
        return {"value": chosen["kind"], "derivation": "copied target tied to named occluding anchor",
                "target_copy_id": chosen["id"], "anchor_blocker_object": q.get("anchor_blocker_object")}
    def count(kind):
        return sum(e.get("kind") == kind and all(
            e.get(k) == q[k] for k in ("pose", "region") if q.get(k) is not None
        ) for e in entities)
    a = count(q["kind"])
    if q["mode"] == "count":
        return {"value": a, "derivation": "declared originals + exported copies", "counts": {q["kind"]: a}}
    b = count(q["other_kind"])
    return {"value": q["kind"] if a > b else q["other_kind"] if b > a else "equal",
            "derivation": "declared originals + exported copies",
            "counts": {q["kind"]: a, q["other_kind"]: b}}
