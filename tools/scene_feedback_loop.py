"""Generate, independently review, repair, and re-review medium scene questions."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "experiments/sol_augment"), str(REPO / "tools")]
import run as augment
import repair_medium
import independent_qa_audit as reviewer
from scene_factory.download import download, glb_header


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def sha(path):
    with Path(path).open("rb") as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()


CODE_SNAPSHOT = {str(path.relative_to(REPO)): path.read_bytes() for path in
                 (Path(__file__), augment.HERE / "run.py", augment.HERE / "worker.py",
                  augment.HERE / "repair_medium.py", REPO / "tools/independent_qa_audit.py",
                  REPO / "scene_factory/rendering.py", REPO / "scene_factory/render_policy.py")}


def medium(result_path):
    result_path = Path(result_path).resolve()
    result = json.loads(result_path.read_text())
    items = [item for item in result.get("items", []) if item.get("level") == "medium"]
    if len(items) != 1:
        raise ValueError("Need one complete medium question")
    item = items[0]
    glb = Path(item["glb"])
    if not glb.is_absolute():
        glb = result_path.parent / glb
    glb_header(glb)
    images = [glb.parent / item[name] for name in ("start_image", "reveal_image")]
    images += sorted(glb.parent.glob("region*.png"))[:2]
    images += sorted(glb.parent.glob("az_*_overview.png"))
    if not item.get("question") or not str(item.get("answer", "")).strip():
        raise ValueError("Missing question or model answer")
    signature = {"question": item["question"], "answer": item["answer"],
                 "camera": item["initial_camera"], "glb": sha(glb),
                 "start": item["start_image"], "reveal": item["reveal_image"],
                 "images": {path.name: sha(path) for path in images}}
    digest = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    return result, item, glb.resolve(), digest


class LazyRemote:
    """Start the configured renderer only when inspection or rebuilding is needed."""
    def __init__(self, name):
        self.remote = augment.Remote(name)
        self.created = False
        self.name = name
        self.worker_sha256 = self.remote.worker_sha256

    def create(self):
        if not self.created:
            self.remote.create()
            self.created = True

    def run(self, *args, **kwargs):
        self.create()
        return self.remote.run(*args, **kwargs)


def reuse_source_inspect(cache, output, source, worker_sha):
    """Reuse verified remote source views; incomplete caches are simply ignored."""
    if not cache or not source or output.exists():
        return False
    cache = Path(cache)
    try:
        marker = json.loads((cache / "fingerprint.json").read_text())
        if marker.get("source_sha256") != sha(source) or marker.get("worker_sha256") != worker_sha:
            return False
        if marker.get("render") != augment.render_settings():
            return False
        names = ["fingerprint.json", "cameras.json", *[view + ".png" for view in augment.VIEWS]]
        if not all((cache / name).is_file() for name in names):
            return False
    except (OSError, ValueError, TypeError):
        return False
    output.mkdir(parents=True)
    for name in names:
        shutil.copy2(cache / name, output / name)
    return True


def seed_result(path):
    path = Path(path).resolve()
    directory, pointer = reviewer.current_run(path.parent)
    if pointer:
        if pointer["status"] != "complete":
            raise ValueError("Seed's current version is not complete")
        path = directory / "result.json"
    state = path.parent / "state.json"
    if state.is_file() and json.loads(state.read_text()).get("status") != "complete":
        raise ValueError("Seed is running, unsuitable, or failed")
    result, _, _, _ = medium(path)
    # Resolve artifact paths without touching the original version.
    for item in result["items"]:
        glb = Path(item["glb"])
        item["glb"] = str((path.parent / glb).resolve() if not glb.is_absolute() else glb)
    return path, result


def finish_generation(directory, result):
    result["terminal"] = True
    save(directory / "result.json", result)
    status = "unsuitable" if result.get("unsuitable") else "error" if result.get("error") else "complete"
    save(directory / "state.json", {"status": status})
    save(directory / "terminal.json", {"finished_at": time.time()})
    augment.set_current_run(directory, directory, status)
    if status != "complete":
        return status
    medium(directory / "result.json")
    return status


def deliver(result_path, audit_path, output):
    _, item, glb, _ = medium(result_path)
    output.mkdir(parents=True, exist_ok=True)
    for source, name in ((glb, "scene.glb"), (glb.parent / item["start_image"], "start.png"),
                         (glb.parent / item["reveal_image"], "reveal.png")):
        target = output / name
        target.unlink(missing_ok=True)
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
    question = {name: item[name] for name in ("question", "answer", "initial_camera")}
    question.update(answer_source="model_visual", review_source="independent_model", gold=False)
    save(output / "question.json", question)
    shutil.copy2(audit_path, output / "audit.json")
    return output


def process(row, args, remote):
    uid = row["uid"]
    seed_path, seed = seed_result(row["result"]) if row.get("result") else (None, None)
    source_name = row.get("source_glb") or row.get("path") or (seed or {}).get("source")
    source = Path(source_name).expanduser().resolve() if isinstance(source_name, str) else None
    if source is not None and not source.is_file():
        source = None
    if source is None and (row.get("url") or row.get("glb_url")):
        source = args.out / "sources" / (uid + ".glb")
        download(row.get("url") or row["glb_url"], source, max_bytes=args.max_bytes)
    if source:
        glb_header(source)
        if source.stat().st_size > args.max_bytes:
            raise ValueError("Source exceeds size budget")
    elif seed is None:
        raise ValueError("Provide source_glb/url or a complete existing result")
    config = {"source": sha(source) if source else None,
              "seed": medium(seed_path)[3] if seed_path else None,
              "model": augment.MODEL, "url": augment.ENDPOINT,
              "max_repairs": args.max_repairs,
              "technical_rounds": 2, "review_cache_policy": "first_valid",
              "code": {name: hashlib.sha256(data).hexdigest() for name, data in CODE_SNAPSHOT.items()}}
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    scene = args.out / uid
    work = scene / ("run_" + fingerprint[:12])
    state_file = work / "loop.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {
        "uid": uid, "status": "running", "fingerprint": fingerprint, "configuration": config, "history": []}
    for name, data in CODE_SNAPSHOT.items():
        snapshot = work / 'workflow_snapshot' / name
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        if not snapshot.exists():
            snapshot.write_bytes(data)
    pointer = scene / "current_loop.json"
    save(pointer, {"run_dir": work.name, "status": "running"})
    if state["status"] in ("keep", "reject"):
        final = state.get("result_path")
        if not final or medium(final)[3] == state.get("medium_signature"):
            save(pointer, {"run_dir": work.name, "status": state["status"]})
            return {**state, "cached": True}
        raise ValueError("Completed artifacts changed; use a new output directory")
    state["status"] = "running"
    save(state_file, state)
    try:
        for number in range(args.max_repairs + 1):
            version = work / f"round_{number}"
            generated = version / uid
            result_path = generated / "result.json"
            completed = generated / "state.json"
            ready = (result_path.exists() and completed.exists()
                     and json.loads(completed.read_text()).get("status") == "complete")
            if not ready:
                if number == 0:
                    if seed:
                        status = finish_generation(generated, {**seed, "seed_result": str(seed_path)})
                    else:
                        reuse_source_inspect(row.get("source_inspect"), generated / "inspect",
                                            source, remote.worker_sha256)
                        remote.create()
                        result = augment.run_scene(remote, source, generated, focus="medium", rounds=2)
                        status = finish_generation(generated, result)
                    if status == "unsuitable":
                        state.update(status="reject", reason=result_path.read_text(), category="unsuitable")
                        break
                    if status != "complete":
                        raise ValueError("Generation failed; inspect " + str(result_path))
                else:
                    previous = work / f"round_{number - 1}"
                    result_path = repair_medium.repair(previous / uid / "result.json",
                                                      previous / "audit" / uid / "record.json",
                                                      generated, remote=remote, source=source)
            if number > 0 and medium(result_path)[3] == medium(work / f"round_{number - 1}" / uid / "result.json")[3]:
                state.update(status="reject", reason="返修未改变中等题或视觉证据", category="no_change")
                state.update(result_path=str(result_path), medium_signature=medium(result_path)[3])
                break
            options = SimpleNamespace(baseline=version, output=version / "audit", level="medium",
                                      require_terminal=True, regions=True, overviews=True, details=False,
                                      model=augment.MODEL, url=augment.ENDPOINT, max_attempts=3,
                                      shared_cache_dir=args.review_cache)
            review = reviewer.audit(uid, options, augment.key())
            audit_path = options.output / uid / "record.json"
            state.update(result_path=str(result_path), audit_path=str(audit_path),
                         medium_signature=medium(result_path)[3])
            event = {"round": number, "result_path": str(result_path), "audit_path": str(audit_path),
                     "status": review["status"], "judgment": review.get("judgment"),
                     "cached": review.get("cached", False)}
            state["history"] = [item for item in state["history"] if item["round"] < number] + [event]
            save(state_file, state)
            if review["status"] != "ok":
                raise RuntimeError("审核调用失败，不能作为质量剔除：" + str(review.get("errors")))
            judgment = review["judgment"]
            if judgment["pass"]:
                folder = deliver(result_path, audit_path, work / "delivery")
                state.update(status="keep", reason=judgment["reason"], folder=str(folder),
                             repairs=number, quality_source="independent_model", gold=False)
                break
            if number == args.max_repairs:
                state.update(status="reject", category="quality", reason=judgment["reason"], repairs=number)
                break
        save(state_file, state)
        save(pointer, {"run_dir": work.name, "status": state["status"]})
        return {**state, "cached": False}
    except Exception as error:
        state.update(status="failed", reason=type(error).__name__ + ": " + str(error))
        save(state_file, state)
        save(pointer, {"run_dir": work.name, "status": "failed"})
        return {**state, "cached": False}


def report(results, output, began):
    summary = {"total": len(results), "keep": sum(r["status"] == "keep" for r in results),
               "reject": sum(r["status"] == "reject" for r in results),
               "failed": sum(r["status"] == "failed" for r in results),
               "cached": sum(r.get("cached", False) for r in results),
               "elapsed_seconds": round(time.time() - began, 2), "level": "medium",
               "model_opinion_not_gold": True, "results": results}
    save(output / "summary.json", summary)
    for status in ("keep", "reject", "failed"):
        (output / (status + ".jsonl")).write_text("".join(
            json.dumps(r, ensure_ascii=False) + "\n" for r in results if r["status"] == status))
    page = ['<!doctype html><meta charset="utf-8"><title>中等题反馈闭环</title>',
            '<style>body{font-family:system-ui;background:#f5f6f8;margin:24px}article{background:white;padding:20px;margin:20px 0}img{width:100%}.views{display:grid;grid-template-columns:1fr 1fr;gap:12px}figure{margin:0}small{overflow-wrap:anywhere}</style>',
            '<h1>中等题：出题 → 审核 → 返修 → 再审</h1><p>模型质量意见，非人工真值。困难题不作为保留门槛。原题和每轮意见均保留。</p>',
            '<p>' + html.escape(json.dumps({k:v for k,v in summary.items() if k != "results"}, ensure_ascii=False)) + '</p>']
    for row in results:
        page.append('<article><h2>' + html.escape(row['uid']) + ' · ' +
                    {'keep':'保留','reject':'剔除','failed':'运行失败'}[row['status']] + '</h2>')
        for event in row.get('history', []):
            try:
                _, item, glb, _ = medium(event['result_path'])
            except (OSError, ValueError, KeyError, TypeError) as error:
                page.append('<p>该轮证据不可用：' + html.escape(str(error)) + '</p>')
                continue
            page.append('<h3>第' + str(event['round']) + '轮</h3><div class="views">')
            for field, label in (('start_image','起始视角'),('reveal_image','揭示视角')):
                src = html.escape(os.path.relpath(glb.parent/item[field], output), quote=True)
                page.append('<figure><img loading="lazy" src="' + src + '"><figcaption>' + label + '</figcaption></figure>')
            page.append('</div><p>题目：' + html.escape(item['question']) + '<br>模型答案：' + html.escape(str(item['answer'])) + '</p>')
            page.append('<p>审核：' + html.escape(json.dumps(event.get('judgment') or {}, ensure_ascii=False)) + '</p>')
        page.append('<p>' + html.escape(row.get('reason','')) + '</p></article>')
    (output / 'gallery.html').write_text('\n'.join(page))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, required=True, help='JSONL: uid + source_glb/url; optional result for existing QA')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--max-repairs', type=int, default=2)
    parser.add_argument('--max-bytes', type=int, default=35*1048576)
    parser.add_argument('--pod', default='scene-sol-feedback-loop')
    parser.add_argument('--review-cache', type=Path, help='shared first-valid-review cache; default OUT/review_cache')
    parser.add_argument('--keep-pod', action='store_true')
    args = parser.parse_args()
    if args.max_repairs < 0:
        parser.error('--max-repairs must be nonnegative')
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    args.review_cache = (args.review_cache or args.out / 'review_cache').resolve()
    rows = [json.loads(line) for line in args.sources.read_text().splitlines() if line.strip()]
    seen = set()
    for row in rows:
        uid = row.get('uid')
        if not isinstance(uid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', uid) or uid in seen:
            parser.error('Each row needs a unique safe uid')
        seen.add(uid)
    remote = LazyRemote(args.pod)
    results, began = [], time.time()
    try:
        for row in rows:
            # An intake failure must not leave yesterday's successful delivery current.
            pointer = args.out / row['uid'] / 'current_loop.json'
            save(pointer, {'run_dir': None, 'status': 'running'})
            try:
                result = process(row, args, remote)
            except Exception as error:
                result = {'uid':row['uid'], 'status':'failed', 'reason':type(error).__name__+': '+str(error)}
                save(pointer, {'run_dir': None, 'status': 'failed', 'reason': result['reason']})
            result['source_metadata'] = row
            results.append(result)
            save(args.out / 'progress.json', {'total':len(rows), 'completed':len(results),
                                             'pending':len(rows)-len(results), 'latest':row['uid']})
            report(results, args.out, began)
            print(json.dumps({'uid':row['uid'], 'status':result['status'], 'completed':len(results)}, ensure_ascii=False), flush=True)
    finally:
        if remote.created and not args.keep_pod:
            remote.remote.close()
    report(results, args.out, began)


if __name__ == '__main__':
    main()
