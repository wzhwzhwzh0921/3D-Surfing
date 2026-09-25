"""Generate an answer-blind, tool-driven browser trial for one scene question."""
from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path

from .supervisor import LEVELS


def render_blind_trial(work: Path, level: str, output: Path) -> dict:
    if level not in LEVELS:
        raise ValueError(f"Unknown level: {level}")
    work = work.resolve()
    output = output.resolve()
    validation = json.loads((work / "output/validation.json").read_text())
    result = next((row for row in validation["results"] if row["level"] == level), None)
    if result is None:
        raise ValueError(f"Validation has no {level} result")
    asset = work / "output" / level / "scene.glb"
    if not asset.is_file() or hashlib.sha256(asset.read_bytes()).hexdigest() != result["sha256"]:
        raise ValueError("GLB differs from the validated asset")
    az = float(result["start_camera"]["azimuth_deg"])
    elevation = float(result["start_camera"]["elevation_deg"])
    # Blender is Z-up; glTF/model-viewer is Y-up. Export maps (x,y,z) to (x,z,-y).
    theta = (90 + az) % 360
    phi = 90 - elevation
    config = {"schema_version": "sceneqa_blind_trial_v1",
              "render_protocol": "modelviewer_perspective_v1",
              "scene_id": validation.get("scene_id") or work.name,
              "level": level, "question": result["question"],
              "asset_sha256": result["sha256"],
              "start_camera_blender": result["start_camera"],
              "start_orbit": f"{theta:g}deg {phi:g}deg auto"}
    template = (Path(__file__).parent / "static/blind_review.html").read_text(encoding="utf-8")
    page = (template.replace("__SCENE_SRC__", html.escape(os.path.relpath(asset, output.parent), quote=True))
            .replace("__CONFIG__", json.dumps(config, ensure_ascii=False).replace("<", "\\u003c")))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")
    return {"output": str(output), "scene_id": config["scene_id"], "level": level,
            "question": config["question"], "asset_sha256": result["sha256"],
            "start_orbit": config["start_orbit"]}
