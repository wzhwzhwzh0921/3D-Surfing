"""Add user requests to frozen, reviewed questions with a configurable renderer."""
from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path
import shutil
import time
from types import SimpleNamespace

from .rendering import render_settings
from .download import glb_header
from .screening.inspection import InspectionRenderer
from .screening.rich import validate_inspection

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = Path(__file__).with_name("prompts")
TYPES = {"color", "count", "identity", "shape", "material", "text", "spatial", "association", "state", "comparison"}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def text(value):
    return isinstance(value, str) and bool(value.strip())


def refs(value, known):
    if not isinstance(value, list) or not value or any(x not in known for x in value):
        raise ValueError("Evidence must reference actual known views")


def validate_plan(plan, original, mode, known):
    if plan.get("action") == "unsuitable" and text(plan.get("reason")):
        return plan
    if plan.get("action") != "propose" or plan.get("mode") != mode or not text(plan.get("followup")):
        raise ValueError("Need a user followup for the requested mode")
    expected = ["original", "extra_1", "extra_2"] if mode == "bundle" else ["original", "final"]
    reqs = plan.get("requirements")
    if not isinstance(reqs, list) or [r.get("id") for r in reqs] != expected:
        raise ValueError("Requirements must preserve original and include the requested additions")
    if reqs[0].get("question") != original:
        raise ValueError("The original question cannot be rewritten")
    for r in reqs:
        if not all(text(r.get(k)) for k in ("question", "target", "blocked_by")) or r.get("type") not in TYPES:
            raise ValueError("Each requirement needs a real question, target, type and occlusion")
        refs(r.get("views"), known)
    hops = plan.get("hops")
    if mode == "bundle":
        if hops != [] or len({r["target"].strip().casefold() for r in reqs}) != 3 or len({r["type"] for r in reqs}) < 2:
            raise ValueError("Bundle needs three distinct targets and at least two types")
    else:
        if not isinstance(hops, list) or [h.get("id") for h in hops] != ["h1", "h2"]:
            raise ValueError("Chain needs two selection hops")
        previous = "original"
        for h in hops:
            candidates = h.get("candidates")
            if h.get("depends_on") != previous or not text(h.get("instruction")):
                raise ValueError("Each hop must use the previous selection")
            if (not isinstance(candidates, list) or len(candidates) < 2 or not all(text(x) for x in candidates)
                    or len({x.strip().casefold() for x in candidates}) != len(candidates)):
                raise ValueError("Each selection needs distinct candidate hypotheses")
            refs(h.get("views"), known)
            previous = h["id"]
        if reqs[-1].get("depends_on") != previous:
            raise ValueError("Final question must depend on the last spatial selection")
    return plan


def validate_answers(value, plan, known):
    expected = [r["id"] for r in plan["requirements"]]
    answers = value.get("answers")
    if not isinstance(answers, list) or [a.get("id") for a in answers] != expected:
        raise ValueError("Every requirement must be answered, including the original")
    for a in answers:
        if not text(a.get("answer")) or type(a.get("clear")) is not bool:
            raise ValueError("Answers need an observation and clear/unclear state")
        refs(a.get("evidence_views"), known)
    hops = value.get("hop_answers")
    if not isinstance(hops, list) or [h.get("id") for h in hops] != [h["id"] for h in plan["hops"]]:
        raise ValueError("Need observations for every spatial hop")
    for h in hops:
        if not text(h.get("selected")):
            raise ValueError("Need actual selected-object descriptions")
        refs(h.get("evidence_views"), known)
    return value


def review_accepted(judgment, plan, answers):
    """Aggregate model judgments only; never infer an answer from geometry."""
    fields = ("pass", "followup_natural", "original_preserved", "additional_work", "cross_view_required")
    if any(type(judgment.get(k)) is not bool for k in fields) or not text(judgment.get("reason")):
        raise ValueError("Review needs explicit verdicts and a reason")
    checks = judgment.get("checks")
    if not isinstance(checks, list) or [c.get("id") for c in checks] != [r["id"] for r in plan["requirements"]]:
        raise ValueError("Review must check all requirements")
    for c in checks:
        if any(type(c.get(k)) is not bool for k in ("pass", "start_hidden")) or not text(c.get("observed_answer")) or not text(c.get("reason")):
            raise ValueError("Need explicit per-requirement checks")
    hops = judgment.get("hop_checks")
    if not isinstance(hops, list) or [h.get("id") for h in hops] != [h["id"] for h in plan["hops"]]:
        raise ValueError("Review must audit each dependency hop")
    for h in hops:
        if (any(type(h.get(k)) is not bool for k in ("pass", "linked_to_previous", "ablation_keeps_ambiguous"))
                or type(h.get("candidate_count")) is not int or not text(h.get("reason"))):
            raise ValueError("Need evidence of candidate ambiguity and necessary hops")
    required = fields if plan["mode"] == "bundle" else tuple(k for k in fields if k != "cross_view_required")
    return (all(judgment[k] for k in required) and all(c["pass"] and c["start_hidden"] for c in checks)
            and all(a["clear"] for a in answers["answers"])
            and all(h["pass"] and h["linked_to_previous"] and h["ablation_keeps_ambiguous"]
                    and h["candidate_count"] >= 2 for h in hops))


def evidence(row):
    source = Path(row["source_glb"])
    glb_header(source)
    capture = Path(row["source_inspect"])
    marker = json.loads((capture / "fingerprint.json").read_text())
    if marker.get("source_sha256") != digest(source):
        raise ValueError("Original source images do not match the source GLB")
    original = json.loads((Path(row["folder"]) / "question.json").read_text())
    start = Path(row["folder"]) / "start.png"
    cameras = json.loads((capture / "cameras.json").read_text())
    known = {"initial": {"label": "initial", "path": str(start), "camera": original["initial_camera"]}}
    for label, camera in cameras.items():
        image = capture / (label + ".png")
        if image.is_file() and digest(image) != digest(start):
            known[label] = {"label": label, "path": str(image), "camera": camera}
    screen = json.loads(Path(row["screen_record"]).read_text())
    for item in screen.get("inspection_views", []):
        if not Path(item["path"]).is_file() or digest(item["path"]) != item.get("image_sha256"):
            raise ValueError("Invalid previous inspection evidence")
        label = "source_" + item["label"]
        known[label] = {**item, "label": label}
    return source, original, known


def task_contract(plan, original, conversation, scene_sha256):
    """Public solver input: selection instructions, never candidate answers."""
    questions = [{"id": r["id"], "question": r["question"]} for r in plan["requirements"]]
    hops = [{k: h[k] for k in ("id", "depends_on", "instruction")} for h in plan["hops"]]
    required = (["original", "extra_1", "extra_2"] if plan["mode"] == "bundle"
                else ["original", "h1", "h2", "final"])
    return {"mode": plan["mode"], "messages": conversation,
            "original_question": original["question"], "user_followup": plan["followup"],
            "initial_camera": original["initial_camera"], "requirements": questions, "hops": hops,
            "required_answer_ids": required, "scene": "scene.glb", "scene_sha256": scene_sha256,
            "grading": "all_requested_answers_and_selections_correct"}


def inspect_cached(renderer, source, folder, inspection):
    """Resume the same rendered observation without re-rendering it."""
    signature = {"source_sha256": digest(source), "worker_sha256": renderer.worker_sha256,
                 "requests": inspection, "render": render_settings()}
    marker = folder / "evidence-cache.json"
    if marker.exists():
        old = json.loads(marker.read_text())
        if old.get("signature") == signature and all(
                Path(v["path"]).is_file() and digest(v["path"]) == v.get("image_sha256")
                for v in old.get("views", [])) and old.get("views"):
            return old["views"]
    views = renderer.render(source, folder, inspection)
    for item in views:
        item["image_sha256"] = digest(item["path"])
    save(marker, {"signature": signature, "views": views})
    return views


def validated_call(model, folder, prompt, images, validator):
    """Repair malformed contracts with new requests, preserving every response."""
    feedback = ""
    for correction in range(3):
        target = folder if correction == 0 else folder.with_name(folder.name + f"_format_{correction}")
        value = model.call(target, prompt, images, feedback=feedback)
        try:
            validator(value)
            return value
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            if correction == 2:
                raise
            feedback = "上一版输出字段或证据引用不合法，请根据同样图片修正JSON格式，不改变问题：" + str(exc)


def run_case(row, mode, out, args, renderer, model):
    source, original, known = evidence(row)
    out.mkdir(parents=True, exist_ok=True)
    originals = {"question": original["question"], "initial_camera": original["initial_camera"]}
    prompts = {name: (PROMPTS / ("followup_" + name + "_zh.txt")).read_text() for name in ("plan", "answer", "audit")}
    signature = {"source_sha256": digest(source), "original": originals, "mode": mode,
                 "model": model.MODEL, "endpoint": model.ENDPOINT, "prompts": prompts,
                 "worker_sha256": renderer.worker_sha256, "render": render_settings(), "implementation_sha256": digest(Path(__file__)),
                 "max_repairs": args.max_repairs, "inspection_rounds": args.inspection_rounds,
                 "images": {k: digest(v["path"]) for k, v in known.items()}}
    fingerprint = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    existing = out / "record.json"
    if existing.exists():
        record = json.loads(existing.read_text())
        if record.get("fingerprint") != fingerprint:
            raise ValueError("Protocol changed; choose a new output directory")
        if record.get("status") in ("keep", "reject"):
            return record
    save(out / "protocol.json", signature)
    feedback, inspection_count = "", 0
    record = {"uid": row["uid"], "name": row["name"], "mode": mode, "fingerprint": fingerprint,
              "source": {k: row.get(k) for k in ("source_url", "download_url", "author_display_name", "license_slug")},
              "original": originals, "baseline_answer": original["answer"], "status": "running", "attempts": [],
              "gold": False, "answer_source": "model_visual", "review_source": "independent_context", "blind_solver_tested": False}
    save(existing, record)
    for attempt in range(args.max_repairs + 1):
        folder = out / f"attempt_{attempt}"
        for turn in range(args.inspection_rounds + 2):
            prompt = (prompts["plan"] + "\n模式：" + mode + "\n原题（必须保留）：" + original["question"]
                      + "\n初始图固定为initial。当前标签：" + ",".join(known)
                      + f"\n本任务剩余取证轮数：{max(0,args.inspection_rounds-inspection_count)}。")
            plan = model.call(folder / f"plan_{turn}", prompt, [(k, Path(v["path"])) for k, v in known.items()], feedback=feedback)
            if plan.get("action") != "inspect":
                try:
                    validate_plan(plan, original["question"], mode, known)
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    feedback += "\n上一版追加计划的字段或引用不合法，请修正：" + str(exc)
                    continue
                break
            if inspection_count >= args.inspection_rounds:
                feedback += "\n真实3D取证预算已用完，依据已有证据出可行追加需求或报告unsuitable。"
                continue
            try:
                inspection = validate_inspection({**plan, "uid": row["uid"]}, row["uid"], known,
                                                SimpleNamespace(views_per_round=2), inspection_count + 1)
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                feedback += "\n取景参数不合法，请自行修正：" + str(exc)
                continue
            inspection_count += 1
            views = inspect_cached(renderer, source, folder / f"inspection_{inspection_count}", inspection)
            for item in views:
                item["image_sha256"] = digest(item["path"])
                known[item["label"]] = item
            save(folder / "evidence.json", list(known.values()))
        else:
            raise ValueError("No final user request after bounded inspection")
        save(folder / "plan.json", plan)
        if plan.get("action") == "unsuitable":
            record.update(status="reject", reason=plan["reason"])
            break
        questions = [{"id": r["id"], "question": r["question"]} for r in plan["requirements"]]
        conversation = [{"role": "user", "content": original["question"]},
                        {"role": "user", "content": plan["followup"]}]
        inputs = {"messages": conversation, "requirements": questions, "hops": plan["hops"]}
        images = [(k, Path(v["path"])) for k, v in known.items()]
        answers = validated_call(model, folder / "answer", prompts["answer"] + "\n" + json.dumps(inputs, ensure_ascii=False),
                                 images, lambda value: validate_answers(value, plan, known))
        save(folder / "answers.json", answers)
        judgment = validated_call(model, folder / "audit", prompts["audit"] + "\n" + json.dumps(
            {"mode": mode, "messages": conversation, "plan_hypotheses": plan, "candidate_answers": answers}, ensure_ascii=False),
            images, lambda value: review_accepted(value, plan, answers))
        accepted = review_accepted(judgment, plan, answers)
        save(folder / "audit.json", judgment)
        record["attempts"].append({"attempt": attempt, "accepted": accepted, "reason": judgment["reason"]})
        record.update(plan=plan, answers=answers, audit=judgment, evidence=list(known.values()), conversation=conversation,
                      reason=judgment["reason"], inspection_rounds=inspection_count)
        save(existing, record)
        if accepted:
            delivery = out / "delivery"
            delivery.mkdir(exist_ok=True)
            scene_path = delivery / "scene.glb"
            if not scene_path.exists():
                try: os.link(source, scene_path)
                except OSError: shutil.copy2(source, scene_path)
            shutil.copy2(known["initial"]["path"], delivery / "start.png")
            task = task_contract(plan, original, conversation, digest(scene_path))
            save(delivery / "task.json", task)
            save(delivery / "answers.json", {**answers, "gold": False, "source": "model_visual"})
            save(delivery / "audit.json", judgment)
            record.update(status="keep", delivery=str(delivery), scene_sha256=digest(scene_path))
            break
        feedback = "独立审核不通过，请只修追加需求，保持原题、初始相机和场景：" + json.dumps(judgment, ensure_ascii=False)
    if record["status"] == "running":
        record["status"] = "reject"
    save(existing, record)
    return record


def gallery(out, records):
    esc = html.escape
    body = []
    for r in records:
        mode = "追加两个独立目标" if r["mode"] == "bundle" else "追加两跳空间定位"
        body.append(f'<article><h2>{esc(r["name"])} · {mode}</h2><p>{"保留" if r["status"]=="keep" else "剔除"} · {esc(r.get("reason", ""))}</p>')
        if r.get("plan"):
            body.append('<div class="message"><strong>用户原题</strong><p>'+esc(r['original']['question'])+'</p><strong>用户追加需求</strong><p>'+esc(r['plan']['followup'])+'</p></div>')
            for a in r.get("answers", {}).get("answers", []):
                body.append('<p><strong>'+esc(a['id'])+'：</strong>'+esc(a['answer'])+'</p>')
            for a in r.get("answers", {}).get("hop_answers", []):
                body.append('<p><strong>定位过程 '+esc(a['id'])+'：</strong>'+esc(a['selected'])+'</p>')
            used = {"initial"}
            for req in r["plan"]["requirements"]: used.update(req["views"])
            for hop in r["plan"]["hops"]: used.update(hop["views"])
            body.append('<div class="images">')
            for v in r.get("evidence", []):
                if v['label'] in used:
                    path = esc(os.path.relpath(v['path'], out), quote=True)
                    body.append(f'<figure><img loading="lazy" src="{path}"><figcaption>{esc(v["label"])}</figcaption></figure>')
            body.append('</div>')
            body.append('<details><summary>独立审核</summary><pre>'+esc(json.dumps(r.get('audit'), ensure_ascii=False, indent=2))+'</pre></details>')
        body.append('</article>')
    (out/'gallery.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>基于原题的用户追加增难</title><style>body{font:16px/1.8 system-ui;background:#edf2f7;color:#172b47;max-width:1200px;margin:30px auto;padding:20px}article{background:white;padding:24px;border-radius:12px;margin:24px 0}.message{background:#eff5ff;padding:18px}.images{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}figure{margin:0}img{width:100%}pre{white-space:pre-wrap;font-size:14px}@media(max-width:700px){.images{grid-template-columns:1fr}}</style><h1>保留原题，追加用户需求</h1><p>原始场景和初始相机不变；新增图片使用配置的渲染后端。答案与审核来自模型，不是人工gold；没有盲测真实操作步数。任意一个要求错误则整题不通过。</p>'+''.join(body)+'</html>')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--modes", nargs="+", choices=("bundle", "chain"), default=["bundle", "chain"])
    parser.add_argument("--uids", nargs="+")
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--max-repairs", type=int, default=1)
    parser.add_argument("--inspection-rounds", type=int, default=2)
    args = parser.parse_args(argv)
    if args.limit < 1 or args.max_repairs < 0 or args.inspection_rounds < 0:
        parser.error("Budgets must be nonnegative and limit positive")
    spec = importlib.util.spec_from_file_location("sceneqa_followup_model", ROOT/'experiments/sol_augment/run.py')
    model = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(model)
    rows = [json.loads(s) for s in (args.campaign/'selected.jsonl').read_text().splitlines() if s.strip()]
    if args.uids: rows = [r for r in rows if r['uid'] in args.uids]
    rows = rows[:args.limit]
    if not rows: parser.error("No selected original questions")
    args.out.mkdir(parents=True, exist_ok=True)
    renderer = InspectionRenderer(args.out)
    records, began = [], time.time()
    try:
        for row in rows:
            for mode in dict.fromkeys(args.modes):
                directory = args.out/row['uid']/mode
                print(json.dumps({'event':'started','name':row['name'],'mode':mode},ensure_ascii=False),flush=True)
                try:
                    record = run_case(row,mode,directory,args,renderer,model)
                except Exception as exc:
                    record={'uid':row['uid'],'name':row['name'],'mode':mode,'status':'failed','reason':type(exc).__name__+': '+str(exc)}
                    save(directory/'failure.json',record)
                records.append(record)
                for status in ('keep','reject','failed'):
                    (args.out/(status+'.jsonl')).write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records if r['status']==status))
                summary={'status':'running','total':len(records),'counts':{s:sum(r['status']==s for r in records) for s in ('keep','reject','failed')},'elapsed_seconds':round(time.time()-began,2),'results':records}
                save(args.out/'summary.json',summary)
                gallery(args.out,records)
                print(json.dumps({'event':'finished','name':row['name'],'mode':mode,'status':record['status'],'reason':record.get('reason')},ensure_ascii=False),flush=True)
    finally:
        renderer.close()
    summary['status']='complete'
    save(args.out/'summary.json',summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
