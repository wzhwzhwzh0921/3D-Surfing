"""Restricted ReAct solver over RGB screenshots and five camera tools."""
from __future__ import annotations

import base64
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import select
import subprocess
import threading
import time
from urllib.parse import quote, urlsplit

import requests

from .blind_review import render_blind_trial
from .supervisor import LEVELS


BRIDGE_SCRIPT = Path(__file__).resolve().parent / "static/blind-agent/browser.cjs"
RETRYABLE = {429, 500, 502, 503, 504}
SYSTEM = """你在一个三维场景中主动探索并回答中文问题。只能依据收到的 RGB 截图和五个相机工具操作；看不到网格名称、材质元数据、生成器答案或额外证据图。
每次最多调用一个工具，并等待新截图；相机工具必须使用最新 observation_id。focus_at 使用从左上角起算的 0..1 画面坐标，点击到遮挡物会聚焦遮挡物。orbit 的 yaw/pitch 单位为度，正 pitch 抬高相机；zoom 的 factor>1 靠近焦点；move 的距离按场景对角线归一化；undo_view 退回上一成功视角，没有一键重置。
能清楚看到决定性证据时 submit_answer(status=observed)；仍不确定但要猜测用 guessed；无法解答用 unsolved。引用实际收到的 observation_id，不要把工具成功或调用次数当成答案证据。"""


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required, "additionalProperties": False}}}


OBSID = {"type": "string", "description": "Latest screenshot observation_id"}
TOOLS = [
    _tool("focus_at", "Focus a visible surface at normalized image coordinates; no automatic zoom.",
          {"observation_id": OBSID, "x": {"type": "number", "minimum": 0, "maximum": 1},
           "y": {"type": "number", "minimum": 0, "maximum": 1}}, ["observation_id", "x", "y"]),
    _tool("orbit", "Rotate around current focus. Angles are continuous degrees; positive pitch raises camera.",
          {"observation_id": OBSID, "yaw": {"type": "number"}, "pitch": {"type": "number"}},
          ["observation_id", "yaw", "pitch"]),
    _tool("zoom", "Change camera distance by a continuous factor; factor > 1 approaches focus.",
          {"observation_id": OBSID, "factor": {"type": "number", "minimum": 0.25, "maximum": 4}},
          ["observation_id", "factor"]),
    _tool("move", "Translate camera and focus together; distance is a fraction of scene diagonal.",
          {"observation_id": OBSID,
           "direction": {"type": "string", "enum": ["left", "right", "up", "down", "forward", "backward"]},
           "distance": {"type": "number", "exclusiveMinimum": 0, "maximum": 2}},
          ["observation_id", "direction", "distance"]),
    _tool("undo_view", "Restore previous successful camera view, producing a new observation_id.",
          {"observation_id": OBSID}, ["observation_id"]),
    _tool("submit_answer", "End this episode with an independent answer and cited observation IDs.",
          {"answer": {"type": "string"},
           "status": {"type": "string", "enum": ["observed", "guessed", "unsolved"]},
           "evidence_observation_ids": {"type": "array", "items": OBSID},
           "evidence_note": {"type": "string"}},
          ["answer", "status", "evidence_observation_ids"]),
]


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def validate_tool(name: str, args: object, current: str, known: set[str]) -> str | None:
    if not isinstance(args, dict):
        return "invalid_arguments"
    if name == "submit_answer":
        required = {"answer", "status", "evidence_observation_ids"}
        if not required <= args.keys() or args.keys() - required - {"evidence_note"}:
            return "invalid_arguments"
        status, evidence = args["status"], args["evidence_observation_ids"]
        if (status not in ("observed", "guessed", "unsolved") or
                not isinstance(args["answer"], str) or
                (status != "unsolved" and not args["answer"].strip()) or
                not isinstance(evidence, list) or
                any(not isinstance(x, str) or x not in known for x in evidence) or
                (status != "unsolved" and not evidence) or
                len(evidence) != len(set(evidence)) or
                ("evidence_note" in args and not isinstance(args["evidence_note"], str))):
            return "invalid_answer_or_evidence"
        return None
    fields = {"focus_at": {"observation_id", "x", "y"},
              "orbit": {"observation_id", "yaw", "pitch"},
              "zoom": {"observation_id", "factor"},
              "move": {"observation_id", "direction", "distance"},
              "undo_view": {"observation_id"}}.get(name)
    if fields is None:
        return "unknown_tool"
    if set(args) != fields:
        return "invalid_arguments"
    if args["observation_id"] != current:
        return "stale_observation"
    if name == "focus_at" and not all(_number(args[k]) and 0 <= args[k] <= 1 for k in ("x", "y")):
        return "invalid_coordinates"
    if name == "orbit" and not all(_number(args[k]) for k in ("yaw", "pitch")):
        return "invalid_angles"
    if name == "zoom" and (not _number(args["factor"]) or not 0.25 <= args["factor"] <= 4):
        return "invalid_factor"
    if name == "move" and (args["direction"] not in ("left", "right", "up", "down", "forward", "backward") or
                           not _number(args["distance"]) or not 0 < args["distance"] <= 2):
        return "invalid_move"
    return None


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@contextmanager
def local_scene_server(work: Path):
    handler = partial(_QuietHandler, directory=str(work.resolve()))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class BrowserBridge:
    def __init__(self, output: Path):
        self.log = (output / "browser.log").open("w")
        self.process = subprocess.Popen(
            ["node", str(BRIDGE_SCRIPT)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
            text=True, bufsize=1)

    def call(self, command: dict, timeout: float = 90) -> dict:
        self.process.stdin.write(json.dumps(command) + "\n")
        self.process.stdin.flush()
        if not select.select([self.process.stdout], [], [], timeout)[0]:
            raise TimeoutError("Browser operation timed out")
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("Browser process exited; see browser.log")
        response = json.loads(line)
        if not response["ok"]:
            raise RuntimeError(response["error"])
        return response["value"]

    def close(self):
        try:
            self.call({"op": "close"}, timeout=15)
            self.process.stdin.close()
            self.process.wait(timeout=15)
        except (OSError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait()
        finally:
            self.log.close()


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _append(path: Path, value: dict) -> None:
    with path.open("a") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def _observation_message(observation: dict) -> dict:
    return {"role": "user", "content": [
        {"type": "text", "text": json.dumps({
            "observation_id": observation["observation_id"],
            "width": observation["width"], "height": observation["height"],
            "undo_available": observation["undo_available"],
            "instruction": "这是当前实际视图。选择一个相机动作，或提交有证据的答案。"}, ensure_ascii=False)},
        {"type": "image_url", "image_url": {"url": observation["image"]}}]}


def _save_frame(output: Path, observation: dict) -> None:
    path = output / "observations" / f"{observation['observation_id']}.png"
    if not path.exists():
        path.write_bytes(base64.b64decode(observation["image"].split(",", 1)[1]))


def _cache_counter(usage: dict | None) -> tuple[int | None, int | None]:
    usage = usage or {}
    total = usage.get("prompt_tokens")
    details = usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens")
    if cached is None:
        cached = usage.get("prompt_cache_hit_tokens", usage.get("cache_read_tokens"))
    if not (type(total) is int and type(cached) is int and 0 <= cached <= total):
        return total, None
    return total, cached


def _request_model(endpoint: str, key: str, model: str, messages: list[dict],
                   cache_key: str, deadline: float, output: Path, number: int,
                   records: list[dict], session: requests.Session) -> dict:
    payload = {"model": model, "messages": messages, "tools": TOOLS,
               "tool_choice": "auto", "parallel_tool_calls": False,
               "max_completion_tokens": 65536}
    headers = {"Authorization": f"Bearer {key}"}
    if urlsplit(endpoint).hostname == "tokenhub.tencentmaas.com":
        payload["prompt_cache_key"] = cache_key
        headers["X-Session-ID"] = cache_key
    for attempt in range(1, 5):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Episode deadline reached")
        started = time.monotonic()
        status, data, retry_after = None, {}, 0.0
        try:
            response = session.post(endpoint, headers=headers, json=payload,
                                    timeout=(min(15, remaining), min(900, remaining)))
            status = response.status_code
            try:
                data = response.json()
            except ValueError:
                data = {"error": "Non-JSON response"}
            if not isinstance(data, dict):
                data = {"error": "Non-object response"}
            try:
                retry_after = min(max(float(response.headers.get("Retry-After", 0)), 0), 300)
            except ValueError:
                pass
        except requests.RequestException as exc:
            data = {"error": type(exc).__name__}
        usage = data.get("usage") if status == 200 else None
        total, cached = _cache_counter(usage)
        record = {"request": number, "attempt": attempt, "status": status,
                  "seconds": round(time.monotonic() - started, 3),
                  "prompt_tokens": total, "cached_tokens": cached,
                  "completion_tokens": (usage or {}).get("completion_tokens")}
        records.append(record)
        _append(output / "usage.jsonl", record)
        if status == 200:
            try:
                return data["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as exc:
                raise ValueError("Model response lacks choices[0].message") from exc
        if status not in (None, *RETRYABLE) or attempt == 4:
            raise RuntimeError(f"Model HTTP {status}; response omitted from logs")
        delay = max(2 ** attempt, retry_after)
        if time.monotonic() + delay >= deadline:
            raise TimeoutError("Episode deadline reached during retry")
        time.sleep(delay)
    raise RuntimeError("Unreachable retry state")


def _usage_summary(records: list[dict]) -> dict:
    successes = [row for row in records if row["status"] == 200]
    measured = [row for row in successes if row["prompt_tokens"] is not None and row["cached_tokens"] is not None]
    total = sum(row["prompt_tokens"] for row in measured)
    return {"api_attempts": len(records), "completed_calls": len(successes),
            "retryable_errors": sum(row["status"] in RETRYABLE or row["status"] is None for row in records),
            "prompt_tokens": sum(row["prompt_tokens"] or 0 for row in successes),
            "cached_tokens": sum(row["cached_tokens"] or 0 for row in measured),
            "completion_tokens": sum(row["completion_tokens"] or 0 for row in successes),
            "cache_measured_calls": len(measured),
            "cache_hit_rate": (sum(row["cached_tokens"] for row in measured) / total if total else None)}


def run_blind_agent(*, work: Path, level: str, output: Path, endpoint: str,
                    key: str, model: str, max_seconds: int = 900) -> dict:
    if level not in LEVELS or max_seconds <= 0:
        raise ValueError("Choose a valid tier and positive deadline")
    parsed = urlsplit(endpoint)
    if (parsed.scheme not in ("http", "https") or not parsed.netloc or
            parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Endpoint must be a credential-free HTTP(S) URL")
    if not key:
        raise ValueError("Missing model API key")
    work = work.resolve()
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"Blind run output already exists: {output}")
    page = work / f"blind-{level}.html"
    config = render_blind_trial(work, level, page)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    (output / "observations").mkdir()
    cache_key = "sceneqa-blind-" + hashlib.sha256(
        json.dumps([model, SYSTEM, TOOLS], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
    _write_json(output / "manifest.json", {"protocol": "sceneqa_blind_agent_v1",
        "model": model, "endpoint": endpoint, "level": level, "work": str(work),
        "question": config["question"], "asset_sha256": config["asset_sha256"],
        "max_output_tokens": 65536, "round_limit": None, "max_seconds": max_seconds,
        "cache_key": cache_key, "browser_protocol": "modelviewer_perspective_v1"})
    deadline = time.monotonic() + max_seconds
    started = time.monotonic()
    bridge = None
    records, events, submission = [], [], None
    turns, status, error, no_progress = 0, "running", None, 0
    known = set()
    session = requests.Session()
    try:
        with local_scene_server(work) as origin:
            bridge = BrowserBridge(output)
            observation = bridge.call({"op": "init", "url": f"{origin}/{quote(page.name)}"},
                                      timeout=min(90, max_seconds))['observation']
            _save_frame(output, observation)
            known.add(observation["observation_id"])
            first = _observation_message(observation)
            first["content"][0]["text"] = config["question"] + "\n\n" + first["content"][0]["text"]
            messages = [{"role": "system", "content": SYSTEM}, first]
            while time.monotonic() < deadline:
                message = _request_model(endpoint, key, model, messages, cache_key,
                                         deadline, output, turns + 1, records, session)
                turns += 1
                messages.append(message)
                calls = message.get("tool_calls") or []
                if not calls:
                    no_progress += 1
                    messages.append({"role": "user", "content": "请调用一个相机工具，或 submit_answer。"})
                    if no_progress >= 8:
                        status = "stalled"
                        break
                    continue
                advanced = False
                for call in calls:
                    name = (call.get("function") or {}).get("name", "")
                    before = observation["observation_id"]
                    try:
                        args = json.loads(call["function"]["arguments"],
                                          parse_constant=lambda _v: (_ for _ in ()).throw(ValueError()))
                        issue = validate_tool(name, args, before, known)
                    except (ValueError, TypeError, KeyError):
                        args, issue = {}, "invalid_json_arguments"
                    if len(calls) != 1:
                        issue = "one_tool_per_turn_required"
                    if issue:
                        result = {"ok": False, "error": issue, "observation_id": before}
                    elif name == "submit_answer":
                        submission = args
                        result = {"ok": True, "episode_finished": True}
                        status = "submitted"
                    else:
                        value = bridge.call({"op": "act", "name": name, "args": args},
                                            timeout=max(1, min(90, deadline - time.monotonic())))
                        result, observation = value["result"], value["observation"]
                        if result.get("ok"):
                            advanced = True
                            known.add(observation["observation_id"])
                            _save_frame(output, observation)
                    event = {"turn": turns, "tool": name, "arguments": args,
                             "result": result, "before": before,
                             "after": observation["observation_id"]}
                    events.append(event)
                    _append(output / "actions.jsonl", event)
                    messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                     "content": json.dumps(result, ensure_ascii=False)})
                if submission:
                    break
                no_progress = 0 if advanced else no_progress + 1
                if no_progress >= 8:
                    status = "stalled"
                    break
                messages.append(_observation_message(observation))
            if not submission and status == "running":
                status = "deadline"
            if bridge:
                browser_trace = bridge.call({"op": "trace"})
                trace = {**browser_trace["config"], "actions": browser_trace["actions"]}
                if submission:
                    trace.update({"model": model, "answer": submission["answer"],
                        "completion_status": submission["status"],
                        "evidence_note": submission.get("evidence_note", ""),
                        "evidence_observation_ids": submission["evidence_observation_ids"],
                        "successful_action_count": len(trace["actions"]),
                        "final_observation_id": observation["observation_id"],
                        "exported_at": datetime.now(timezone.utc).isoformat()})
                    _write_json(output / "blind-trace.json", trace)
                else:
                    _write_json(output / "partial-trace.json", {**trace, "model": model,
                        "run_status": status, "successful_action_count": len(trace["actions"]),
                        "final_observation_id": observation["observation_id"]})
    except KeyboardInterrupt:
        status, error = "interrupted", "Interrupted"
    except Exception as exc:
        status, error = "error", f"{type(exc).__name__}: {str(exc).replace(key, '[REDACTED]')}"
    finally:
        if bridge:
            bridge.close()
        session.close()
    camera_events = [event for event in events if event["tool"] != "submit_answer"]
    summary = {"protocol": "sceneqa_blind_agent_v1", "model": model,
               "scene_id": config["scene_id"], "level": level,
               "status": status, "error": error,
               "seconds": round(time.monotonic() - started, 2), "model_turns": turns,
               "camera_calls": len(camera_events),
               "successful_camera_calls": sum(bool(event["result"].get("ok")) for event in camera_events),
               "tools_called": dict(Counter(event["tool"] for event in events)),
               "observations": len(known), "submission": submission,
               "usage": _usage_summary(records), "accuracy_judged": False}
    _write_json(output / "summary.json", summary)
    return summary
