"""Strict, inspectable plan contract shared by agent and validator."""
from __future__ import annotations

import json
from pathlib import Path

LEVELS = ("simple", "medium", "hard")


def read_plan(path: str | Path) -> dict:
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    if plan.get("schema_version") != "1.0":
        raise ValueError("schema_version must be 1.0")
    variants = plan.get("variants")
    if not isinstance(variants, list) or len(variants) != 3:
        raise ValueError("Exactly three variants are required")
    if [v.get("level") for v in variants] != list(LEVELS):
        raise ValueError("Variant order must be simple, medium, hard")
    kinds, questions = [], []
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
        q = v.get("question", {})
        if q.get("mode") not in ("count", "compare") or not q.get("text"):
            raise ValueError(f"{level}: count/compare question and text required")
        if q.get("kind") not in {e["kind"] for e in entities}:
            raise ValueError(f"{level}: target kind absent from entity registry")
        if q["mode"] == "compare" and q.get("other_kind") not in {e["kind"] for e in entities}:
            raise ValueError(f"{level}: comparison kind absent")
        if level == "hard" and q["mode"] != "compare":
            raise ValueError("hard: use a comparison requiring two groups")
        cam = v.get("start_camera", {})
        if not isinstance(cam.get("azimuth_deg"), (int, float)) or not isinstance(cam.get("elevation_deg"), (int, float)):
            raise ValueError(f"{level}: explicit starting camera required")
        if not 5 <= cam["elevation_deg"] <= 80:
            raise ValueError(f"{level}: starting elevation must be 5..80 degrees")
        kinds.append(q["kind"])
        questions.append(q["text"].strip())
    if len(set(kinds)) != 3 or len(set(questions)) != 3:
        raise ValueError("Three variants must target different object kinds and question text")
    return plan


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
