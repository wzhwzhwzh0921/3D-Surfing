"""Copy the final generated artifacts into a small, self-contained delivery."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import struct


def checked_glb(path: Path) -> None:
    with path.open("rb") as stream:
        magic, version, size = struct.unpack("<4sII", stream.read(12))
    if magic != b"glTF" or version != 2 or size != path.stat().st_size:
        raise ValueError(f"invalid GLB: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sources", type=Path, help="optional source/author/license JSONL keyed by uid")
    parser.add_argument("--only-model-pass", action="store_true")
    args = parser.parse_args()
    work, out = args.work.resolve(), args.out.resolve()
    if work == out or out.is_relative_to(work):
        raise SystemExit("Delivery must be a separate directory from work")
    out.mkdir(parents=True, exist_ok=True)
    sources = {}
    if args.sources:
        source_rows = [json.loads(line) for line in args.sources.read_text().splitlines() if line.strip()]
        sources = {row["uid"]: row for row in source_rows}
    logs = list(work.glob("worker_*.log"))
    completed = set()
    for log in logs:
        for line in log.read_text().splitlines():
            try:
                event = json.loads(line)
                if "scene" in event and ("pass" in event or "error" in event):
                    completed.add(event["scene"])
            except ValueError:
                continue
    manifest, skipped, inspected = [], [], set()
    scene_dirs = {path.parent for pattern in ("*/result.json", "*/current_run.json")
                  for path in work.glob(pattern)}
    for scene_dir in sorted(scene_dirs):
        uid = scene_dir.name
        inspected.add(uid)
        try:
            pointer_file = scene_dir / "current_run.json"
            run_dir = scene_dir
            if pointer_file.exists():
                pointer = json.loads(pointer_file.read_text())
                relative = pointer.get("run_dir", ".")
                if not isinstance(relative, str) or Path(relative).is_absolute():
                    raise ValueError("invalid current run directory")
                run_dir = (scene_dir / relative).resolve()
                if not run_dir.is_relative_to(scene_dir.resolve()):
                    raise ValueError("current run directory escapes scene directory")
                if pointer.get("status") != "complete":
                    skipped.append({"uid": uid, "reason": "current run: " + str(pointer.get("status"))})
                    continue
            result_file = run_dir / "result.json"
            result = json.loads(result_file.read_text())
            state_file = result_file.parent / "state.json"
            if state_file.exists():
                state = json.loads(state_file.read_text())
                if state.get("status") != "complete":
                    skipped.append({"uid": uid, "reason": "not complete: " + str(state.get("status"))})
                    continue
            if "terminal" in result and result["terminal"] is not True:
                skipped.append({"uid": uid, "reason": "no terminal result"})
                continue
            if not pointer_file.exists() and logs and uid not in completed:
                skipped.append({"uid": uid, "reason": "still running or repairing"})
                continue
            items = result.get("items", [])
            if len(items) != 3 or {item["level"] for item in items} != {"simple", "medium", "hard"}:
                skipped.append({"uid": uid, "reason": result.get("unsuitable") or "no complete three-tier result"})
                continue
            if args.only_model_pass and not all(item.get("model_pass") is True for item in items):
                skipped.append({"uid": uid, "reason": "model self-review did not pass all three tiers"})
                continue
            files = []
            for item in items:
                tier = result_file.parent / f"round_{result['iteration']}" / "built" / item["level"]
                source_model = tier / "scene.glb"
                checked_glb(source_model)
                start, reveal = tier / item["start_image"], tier / item["reveal_image"]
                if not start.is_file() or not reveal.is_file():
                    raise ValueError("missing start/reveal evidence")
                if not isinstance(item.get("question"), str) or not isinstance(item.get("answer"), (str, int, float)):
                    raise ValueError("question must be text and answer must be text or a number")
                item["answer"] = str(item["answer"])
                if not item["question"].strip() or not item["answer"].strip():
                    raise ValueError("empty question or answer")
                files.append((item, source_model, start, reveal))
            for item, model, start, reveal in files:
                target = out / uid / item["level"]
                target.mkdir(parents=True, exist_ok=True)
                for source, name in ((model, "scene.glb"), (start, "start.png"), (reveal, "reveal.png")):
                    shutil.copy2(source, target / name)
                question = {"question": item["question"], "answer": item["answer"],
                            "initial_camera": item["initial_camera"],
                            "model_pass": item.get("model_pass"),
                            "reason": item.get("reason", ""), "answer_source": "model_visual"}
                if item.get("target"):
                    question["target"] = item["target"]
                (target / "question.json").write_text(json.dumps(question, ensure_ascii=False, indent=2))
                manifest.append({"uid": uid, "level": item["level"],
                                 "folder": str(target.relative_to(out)), **question})
            audit = run_dir / "independent_audit.json"
            if audit.is_file():
                shutil.copy2(audit, out / uid / "independent_audit.json")
        except (ValueError, KeyError, OSError, struct.error, TypeError) as exc:
            skipped.append({"uid": uid, "reason": str(exc)})
    attempted = completed | {path.parent.name for path in work.glob("*/state.json")} | {
        path.parent.name for path in work.glob("*/error.txt")
    }
    for uid in sorted(attempted - inspected):
        error = work / uid / "error.txt"
        skipped.append({"uid": uid, "reason": error.read_text().strip() or "failed without final result"
                        if error.is_file() else "no final result"})
    (out / "questions.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in manifest))
    used = {row["uid"] for row in manifest}
    (out / "sources.jsonl").write_text("".join(json.dumps(sources[uid], ensure_ascii=False) + "\n"
                                               for uid in sorted(used) if uid in sources))
    (out / "skipped.json").write_text(json.dumps(skipped, ensure_ascii=False, indent=2))
    print(json.dumps({"scene_count": len({row["uid"] for row in manifest}),
                      "question_count": len(manifest), "skipped": len(skipped),
                      "quality_status": "model-generated; model reviews are not gold"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
