"""Bounded VLM inspection of native scene richness; with a configurable renderer.

Numbers and semantic descriptions are model estimates. Validators enforce the
protocol and evidence references, never claim to count semantic objects in GLB.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

from ..rendering import render_settings
from .judge import failed_record, write_record
from .policy import VIEWS


def settings(config):
    if config.version == "v5":
        return dict(acceptance="exploration_evidence", count_thresholds="descriptive_only",
                    min_questions=3, min_question_types=2, min_targets=3, min_regions=2,
                    inspection_rounds=config.inspection_rounds, views_per_round=config.views_per_round)
    return {name: getattr(config, name) for name in
            ("min_objects", "min_categories", "min_regions", "inspection_rounds", "views_per_round")}


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(uid, config, paths, source, worker_sha):
    glb = source.get("source_glb") or source.get("path")
    protocol = {"uid": uid, "prompt": config.prompt, "model": config.model,
                "url": config.url, "settings": settings(config), "renderer_sha256": worker_sha, "render":render_settings(),
                "glb_sha256": digest_file(glb) if glb and Path(glb).is_file() else None,
                "images": [digest_file(path) for path in paths],
                "temperature": 0, "max_tokens": 4000,
                "schema": "exploration-v5-1" if config.version == "v5" else "rich-v4-1"}
    return hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()


def nonblank(value):
    return isinstance(value, str) and bool(value.strip())


def number(value, lower, upper):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and lower <= value <= upper)


def references(value, known):
    if (not isinstance(value, list) or not value or
            any(not isinstance(label, str) or label not in known for label in value)):
        raise ValueError("Each evidence list needs known view labels")


def validate_inspection(value, uid, known, config, round_number):
    if value.get("uid") != uid or value.get("action") != "inspect":
        raise ValueError("Inspection requires the given uid and action=inspect")
    requests = value.get("requests")
    if not isinstance(requests, list) or not 1 <= len(requests) <= config.views_per_round:
        raise ValueError("Too many or missing inspection requests")
    trusted = []
    for index, item in enumerate(requests, 1):
        if not isinstance(item, dict) or item.get("reference_view") not in known:
            raise ValueError("Unknown reference_view")
        box = item.get("bbox")
        if (not isinstance(box, list) or len(box) != 4 or not all(number(n, 0, 1) for n in box)
                or box[0] >= box[2] or box[1] >= box[3]):
            raise ValueError("bbox needs ordered normalized x1,y1,x2,y2")
        if not all(number(item.get(name), *bounds) for name, bounds in
                   (("yaw", (-180, 180)), ("pitch", (-75, 75)), ("zoom", (.5, 6)))):
            raise ValueError("yaw [-180,180], pitch [-75,75], zoom [0.5,6] must be finite numbers")
        if not nonblank(item.get("purpose")):
            raise ValueError("Inspection purpose is required")
        request = {name: item[name] for name in ("reference_view", "bbox", "yaw", "pitch", "zoom", "purpose")}
        request["label"] = f"inspect_{round_number}_{index}"
        camera = known[item["reference_view"]].get("camera")
        if camera:
            request["reference_camera"] = camera
        trusted.append(request)
    return trusted


def validate_decision(value, uid, known, config):
    if (value.get("uid") != uid or value.get("action") != "decide" or
            value.get("decision") not in ("keep", "reject") or not nonblank(value.get("reason"))):
        raise ValueError("Decision needs given uid, action=decide, keep/reject, and a reason")
    total = value.get("object_count_lower_bound")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ValueError("object_count_lower_bound must be a nonnegative integer")
    used = set()
    for field in ("categories", "regions", "exploration_evidence"):
        items = value.get(field)
        if not isinstance(items, list):
            raise ValueError(field + " must be an array")
        names = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError(field + " must contain objects")
            name = item.get("description" if field == "exploration_evidence" else "name")
            if not nonblank(name) or name.strip().casefold() in names:
                raise ValueError(field + " requires distinct nonblank descriptions")
            names.add(name.strip().casefold())
            references(item.get("views"), known)
            used.update(item["views"])
            if field == "categories":
                count = item.get("count_lower_bound")
                if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                    raise ValueError("Category counts must be positive integers")
            if field == "regions":
                contents = item.get("contents")
                if not isinstance(contents, list) or not contents or not all(nonblank(x) for x in contents):
                    raise ValueError("Region needs actual contents")
    if sum(item["count_lower_bound"] for item in value["categories"]) != total:
        raise ValueError("Category lower bounds must sum to object_count_lower_bound")
    if value["decision"] == "keep":
        if config.version != "v5" and (total < config.min_objects or len(value["categories"]) < config.min_categories or
                len(value["regions"]) < config.min_regions):
            raise ValueError("keep cannot be below trial thresholds; do not inflate estimates to pass")
        if not value["exploration_evidence"] or not any(label.startswith("inspect_") for label in used):
            raise ValueError("keep requires a real local 3D inspection and cited exploration evidence")
    fields = ["uid", "action", "decision", "reason", "object_count_lower_bound",
              "categories", "regions", "exploration_evidence"]
    if config.version == "v5":
        if value.get("environment") not in ("indoor", "outdoor", "mixed", "unknown"):
            raise ValueError("v5 requires a visual environment label")
        if value.get("scene_quality") not in ("usable", "unusable"):
            raise ValueError("v5 requires scene_quality")
        opportunities = value.get("opportunities")
        if not isinstance(opportunities, list):
            raise ValueError("v5 requires opportunities array")
        for item in opportunities:
            if (not isinstance(item, dict) or not all(nonblank(item.get(k)) for k in
                ("question", "answer", "target", "exploration_requirement")) or
                item.get("question_type") not in ("color", "count", "identity", "shape", "material",
                                                  "text", "spatial", "association", "state", "comparison")):
                raise ValueError("Each opportunity needs a question, observed answer, target, type and exploration reason")
            references(item.get("views"), known)
        if value["decision"] == "keep":
            if (value["environment"] == "unknown" or value["scene_quality"] != "usable" or
                len(value["regions"]) < 2 or len(opportunities) < 3 or
                len({x["question_type"] for x in opportunities}) < 2 or
                len({x["target"].strip().casefold() for x in opportunities}) < 3 or
                not any(label.startswith("inspect_") for x in opportunities for label in x["views"])):
                raise ValueError("v5 keep requires a usable scene, confirmed environment and 3 varied evidence-backed questions")
        fields.extend(("environment", "scene_quality", "opportunities"))
    return {key: value[key] for key in fields}


def image_parts(records):
    parts = []
    for item in records:
        parts.extend([{"type": "text", "text": "视角标签：" + item["label"]},
                      {"type": "image_url", "image_url": {"url": "data:image/png;base64," +
                        base64.b64encode(Path(item["path"]).read_bytes()).decode(), "detail": "high"}}])
    return parts


def call(messages, config, secret, folder, turn, record):
    payload = {"model": config.model, "temperature": 0, "max_tokens": 4000, "messages": messages}
    for attempt in range(1, 4):
        record["attempts"] = record.get("attempts", 0) + 1
        delay = 2 ** attempt
        request = urllib.request.Request(config.url, data=json.dumps(payload).encode(), headers={
            "Authorization": "Bearer " + secret, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                raw = response.read().decode()
            (folder / f"raw_{turn}_{attempt}.json").write_text(raw)
            body = json.loads(raw)
            record["usage"].append(body.get("usage", {}))
            record["served_model"] = body.get("model")
            content = body["choices"][0]["message"]["content"].strip()
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
            value = json.loads(content)
            if not isinstance(value, dict):
                raise ValueError("Response must be a JSON object")
            return value
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace").replace(secret, "[redacted]")
            (folder / f"error_{turn}_{attempt}.txt").write_text(detail[:4000])
            record["errors"].append("HTTP " + str(exc.code) + ": " + detail[:400])
            if exc.code not in (429, 500, 502, 503, 504):
                raise RuntimeError("HTTP " + str(exc.code)) from None
            try:
                delay = max(delay, min(float(exc.headers.get("Retry-After", "0")), 60))
            except ValueError:
                pass
        except Exception as exc:
            record["errors"].append((type(exc).__name__ + ": " + str(exc)).replace(secret, "[redacted]")[:250])
        if attempt < 3:
            time.sleep(delay)
    raise RuntimeError("Model request failed after three attempts")


def judge_rich_scene(uid, config, secret, source, *, renderer=None):
    from .inspection import InspectionRenderer, worker_bundle
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", uid):
        raise ValueError("Unsafe uid")
    source = source or {}
    folder = config.output / config.version / uid
    folder.mkdir(parents=True, exist_ok=True)
    paths = [config.captures / uid / f"{view}.png" for view in VIEWS]
    owned_renderer = renderer is None
    record = failed_record(uid, source, "")
    record.update(errors=[], model=config.model, endpoint=config.url, settings=settings(config),
                  inspection_views=[], machine_estimates=True)
    started = time.time()
    try:
        worker_sha = renderer.worker_sha256 if renderer else hashlib.sha256(worker_bundle()).hexdigest()
        record["fingerprint"] = fingerprint(uid, config, paths, source, worker_sha)
        path = folder / "record.json"
        if path.exists() and not config.force:
            try:
                previous = json.loads(path.read_text())
                evidence_ok = all(Path(item["path"]).is_file() and digest_file(item["path"]) == item.get("image_sha256")
                                  for item in previous.get("inspection_views", []))
            except (ValueError, OSError, KeyError, TypeError):
                previous, evidence_ok = {}, False
            if previous.get("status") == "ok" and previous.get("fingerprint") == record["fingerprint"] and evidence_ok:
                return {**previous, "cached": True, "source": source}
        if path.exists():
            archive = folder / f"attempt_{time.time_ns()}"
            archive.mkdir()
            for old in list(folder.iterdir()):
                if old != archive and not old.name.startswith("attempt_"):
                    old.replace(archive / old.name)
        heading = "本次取证配置：" if config.version == "v5" else "本次试行门槛："
        prompt = (config.prompt + '\n场景ID："' + uid + '"。\n' + heading +
                  json.dumps(settings(config), ensure_ascii=False) +
                  "\n旋转范围yaw[-180,180]度、pitch[-75,75]度，zoom[0.5,6]。仅用图像证据，不能虚增统计。")
        (folder / "prompt.txt").write_text(prompt)
        known = {view: {"label": view, "path": str(path)} for view, path in zip(VIEWS, paths)}
        messages = [{"role": "user", "content": [{"type": "text", "text": prompt}] + image_parts(known.values())}]
        history, inspections, corrections = [], 0, 0
        for turn in range(1, config.inspection_rounds + 4):
            value = call(messages, config, secret, folder, turn, record)
            messages.append({"role": "assistant", "content": json.dumps(value, ensure_ascii=False)})
            history.append({"role": "assistant", "content": value})
            try:
                if value.get("action") == "decide":
                    record["judgment"] = validate_decision(value, uid, known, config)
                    record["status"] = "ok"
                    break
                requests = validate_inspection(value, uid, known, config, inspections + 1)
            except ValueError as exc:
                corrections += 1
                if corrections > 2:
                    raise ValueError("Repeated protocol errors: " + str(exc)) from None
                feedback = "格式/证据未达协议：" + str(exc) + "。请修正，或如实reject；不能编造数量。"
                messages.append({"role": "user", "content": feedback})
                history.append({"role": "user", "content": feedback})
                continue
            if inspections >= config.inspection_rounds:
                raise ValueError("insufficient_evidence: inspection budget exhausted")
            glb = source.get("source_glb") or source.get("path")
            if not glb or not Path(glb).is_file():
                raise ValueError("inspection_unavailable: provide the original source_glb for real 3D inspection")
            if renderer is None:
                renderer = InspectionRenderer(config.output, pod=config.render_pod)
            inspections += 1
            views = renderer.render(Path(glb), folder / f"inspection_{inspections}", requests)
            for item in views:
                item["image_sha256"] = digest_file(item["path"])
                known[item["label"]] = item
            record["inspection_views"].extend(views)
            feedback = f"真实3D新视图；已用{inspections}/{config.inspection_rounds}轮取证。请判断内容丰富度，必要时继续取证。"
            messages.append({"role": "user", "content": [{"type": "text", "text": feedback}] + image_parts(views)})
            history.append({"role": "user", "text": feedback, "views": views})
        if record["status"] != "ok":
            raise ValueError("insufficient_evidence: no valid final decision within turn budget")
    except Exception as exc:
        record["errors"].append((type(exc).__name__ + ": " + str(exc)).replace(secret, "[redacted]")[:500])
    finally:
        if owned_renderer and renderer:
            try:
                renderer.close()
            except Exception as exc:
                record["cleanup_error"] = type(exc).__name__
    record["elapsed_seconds"] = round(time.time() - started, 2)
    (folder / "history.json").write_text(json.dumps(locals().get("history", []), ensure_ascii=False, indent=2))
    write_record(folder, record)
    return record
