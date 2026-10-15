"""One image-based judgment, with evidence fingerprints, cache and retries."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.request

from .policy import VIEWS


@dataclass(frozen=True)
class ScreenConfig:
    captures: Path
    output: Path
    model: str
    url: str
    version: str
    prompt: str
    force: bool = False
    min_objects: int = 20
    min_categories: int = 8
    min_regions: int = 3
    inspection_rounds: int = 2
    views_per_round: int = 2
    render_pod: str | None = None

    def __post_init__(self):
        for name in ("min_objects", "min_categories", "min_regions", "inspection_rounds", "views_per_round"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(name + " must be a positive integer")


def validate_judgment(value: dict, uid: str) -> dict:
    """Validate the response shape only; do not infer asset quality in code."""
    if (not isinstance(value, dict)
            or value.get("uid") != uid
            or value.get("decision") not in ("keep", "reject")
            or not isinstance(value.get("reason"), str)
            or not value["reason"]):
        raise ValueError("invalid simple judgment")
    return {name: value[name] for name in ("uid", "decision", "reason")}


def evidence_fingerprint(uid: str, config: ScreenConfig, paths: list[Path]) -> str:
    """Preserve the previous fingerprint byte order for existing caches."""
    protocol = config.prompt + "\nUID delimiter v2\n" + config.model + config.url
    content = protocol.encode() + uid.encode()
    content += b"".join(hashlib.sha256(path.read_bytes()).digest() for path in paths)
    return hashlib.sha256(content).hexdigest()


def failed_record(uid: str, source: dict, error: str) -> dict:
    return {"uid": uid, "status": "failed", "cached": False, "usage": [],
            "source": source, "machine_only": True, "errors": [error]}


def write_record(folder: Path, record: dict) -> None:
    (folder / "record.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def judge_scene(uid: str, config: ScreenConfig, secret: str, source: dict | None, *, renderer=None) -> dict:
    if config.version in ("v4", "v5"):
        from .rich import judge_rich_scene
        return judge_rich_scene(uid, config, secret, source, renderer=renderer)
    folder = config.output / config.version / uid
    folder.mkdir(parents=True, exist_ok=True)
    paths = [config.captures / uid / f"{view}.png" for view in VIEWS]
    try:
        fingerprint = evidence_fingerprint(uid, config, paths)
    except OSError as exc:
        record = failed_record(uid, source or {}, "missing/unreadable image: " + str(exc))
        write_record(folder, record)
        return record

    record_path = folder / "record.json"
    if record_path.exists() and not config.force:
        try:
            previous = json.loads(record_path.read_text())
        except (OSError, ValueError):
            previous = {}
        if previous.get("status") == "ok" and previous.get("fingerprint") == fingerprint:
            metadata = source if source is not None else previous.get("source", {})
            return {**previous, "cached": True, "source": metadata}
    if record_path.exists():
        archive = folder / f"attempt_{time.time_ns()}"
        archive.mkdir()
        for old in list(folder.iterdir()):
            if old.is_file():
                old.replace(archive / old.name)

    user_prompt = config.prompt + '\n场景ID（仅复制引号内内容）："' + uid + '"。\n'
    (folder / "prompt.txt").write_text(user_prompt + "\n" + ",".join(VIEWS), encoding="utf-8")
    parts = [{"type": "text", "text": user_prompt}]
    for view, path in zip(VIEWS, paths):
        parts.extend([
            {"type": "text", "text": "\n视角标签：" + view + "\n"},
            {"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode(),
                "detail": "high"}},
        ])
    payload = {"model": config.model, "temperature": 0, "max_tokens": 1200,
               "messages": [{"role": "user", "content": parts}]}
    record = {"uid": uid, "status": "failed", "model": config.model,
              "fingerprint": fingerprint, "machine_only": True,
              "endpoint": config.url, "cached": False, "usage": [], "errors": []}
    started = time.time()
    for attempt in range(1, 4):
        record["attempts"] = attempt
        retry_after = 0
        request = urllib.request.Request(
            config.url, data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                raw = response.read().decode()
            (folder / f"raw_{attempt}.json").write_text(raw, encoding="utf-8")
            body = json.loads(raw)
            record["usage"].append(body.get("usage", {}))
            record["served_model"] = body.get("model")
            content = body["choices"][0]["message"]["content"].strip()
            content = content.removeprefix("```json").removesuffix("```").strip()
            record["judgment"] = validate_judgment(json.loads(content), uid)
            record["status"] = "ok"
            break
        except urllib.error.HTTPError as exc:
            (folder / f"error_{attempt}.json").write_bytes(exc.read())
            record["errors"].append("HTTP " + str(exc.code))
            if exc.code not in (429, 500, 502, 503, 504):
                break
            try:
                retry_after = float(exc.headers.get("Retry-After", "0"))
            except ValueError:
                retry_after = 0
        except Exception as exc:
            record["errors"].append(type(exc).__name__ + ": " + str(exc)[:200])
        if attempt < 3:
            time.sleep(max(retry_after, 2 ** attempt))
    record["source"] = source or {}
    record["elapsed_seconds"] = round(time.time() - started, 2)
    write_record(folder, record)
    return record
