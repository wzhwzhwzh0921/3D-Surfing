"""Conservative cheap screen before paying for a long scene-planning session.

Only definitive, well-named low-diversity cases are rejected. Generic mesh names
remain unknown and are passed to the agent for visual inspection.
"""
from __future__ import annotations

import re

STRUCTURAL = {
    "house", "land", "water", "dock", "docks", "door", "step", "steps",
    "beam", "porch", "roof", "path", "floor", "wall", "terrain", "ground",
    "sky", "ceiling", "window", "pillar", "stairs", "stair", "foundation",
    "bridge", "main big beam", "base", "platform", "building", "room",
    "background", "groundplane",
}
GENERIC = {"object", "cube", "mesh", "plane", "cylinder", "sphere", "circle",
           "torus", "collection", "node", "primitive", "pcube", "pplane",
           "pcylinder", "pcone", "pasted", "polysurface"}


def normalized_kind(name: str) -> str:
    name = name.strip()
    # Maya/glTF exports can prepend a namespace and polygon ID before the
    # meaningful object name, e.g. Room:polySurface42_Room:Book_Cook_0.
    # Keep the last namespace component so Book is recognized as a prop.
    name = name.rsplit(":", 1)[-1]
    if name.lower().startswith(("prop_", "sm_")):
        name = name.split("_", 1)[1]
    elif name.lower().startswith("background_"):
        return "background"
    name = name.split("_", 1)[0]
    name = re.sub(r"(?:\.\d+|\s+\d+|\d+)+$", "", name).strip().lower()
    name = re.sub(r"[-]+", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def screen(inventory: dict) -> dict:
    meshes = inventory.get("meshes", [])
    names = [normalized_kind(m["name"]) for m in meshes]
    categories = sorted({n for n in names if n and n not in STRUCTURAL and
                         n.split()[0] not in STRUCTURAL and n.split()[0] not in GENERIC})
    generic = [n for n in names if n and n.split()[0] in GENERIC]
    # A mesh may be generically named despite representing a useful prop. Avoid
    # rejecting such assets without a visual look.
    scene_bounds = inventory.get("bounds") or {}
    small = []
    if "min" in scene_bounds and "max" in scene_bounds:
        span = max(b - a for a, b in zip(scene_bounds["min"], scene_bounds["max"]))
        if span > 0:
            for mesh in meshes:
                mesh_bounds = mesh.get("bounds")
                if mesh_bounds and max(b - a for a, b in zip(*mesh_bounds)) <= 0.45 * span:
                    small.append(mesh["name"])
    geometry_reject = bool(scene_bounds) and len(small) < 2
    status = ("likely_unsuitable" if geometry_reject or len(categories) < 2 and not generic
              else "needs_agent_review")
    return {"status": status, "named_prop_categories": categories,
            "definitive": geometry_reject,
            "generic_mesh_count": len(generic), "mesh_count": len(names),
            "small_mesh_count": len(small),
            "small_mesh_examples": small[:20],
            "rule": ("fewer than two individually small meshes" if geometry_reject else
                     "fewer than two distinct nonstructural named kinds" if status == "likely_unsuitable"
                     else "no definitive low-diversity rejection; agent must inspect actual props")}
