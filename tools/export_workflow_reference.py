"""Export public workflow prompts from source without importing model workers."""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_tree(relative):
    return ast.parse((ROOT / relative).read_text(encoding="utf-8"))


def literal_assignment(relative, name, *, function=None):
    tree = read_tree(relative)
    nodes = tree.body if function is None else next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function)
    nodes = nodes if isinstance(nodes, list) else ast.walk(nodes)
    for node in nodes:
        if (isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            return node.value.value, node.lineno
    raise ValueError(f"No literal {name} in {relative}:{function}")


def template(node, values):
    """Expand only string additions; dynamic expressions remain labeled slots."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in values:
        return values[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return template(node.left, values) + template(node.right, values)
    return "{{运行时填入：" + ast.unparse(node) + "}}"


def assignment_node(relative, name, function):
    owner = next(node for node in read_tree(relative).body
                 if isinstance(node, ast.FunctionDef) and node.name == function)
    return next(node for node in ast.walk(owner) if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == name for target in node.targets))


def final_review_prompt(relative):
    """Resolve the actual final PROMPT, including its source-level replacement."""
    tree = read_tree(relative)
    assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "PROMPT" for target in node.targets)]
    original = ast.literal_eval(assignments[0].value)
    replacement = assignments[1].value
    if not (isinstance(replacement, ast.BinOp) and isinstance(replacement.op, ast.Add)
            and isinstance(replacement.left, ast.Subscript)
            and isinstance(replacement.left.slice, ast.Slice)):
        raise ValueError("Final-review prompt assembly changed; update the exporter")
    stop = replacement.left.slice.upper
    if not (isinstance(stop, ast.Call) and isinstance(stop.func, ast.Attribute)
            and isinstance(stop.func.value, ast.Name) and stop.func.value.id == "PROMPT"
            and stop.func.attr == "index" and len(stop.args) == 1
            and ast.literal_eval(stop.args[0]) == "只输出JSON"):
        raise ValueError("Unexpected final-review prompt slicing")
    return original[:original.index("只输出JSON")] + ast.literal_eval(replacement.right), assignments[1].lineno


def export_reference():
    run = "experiments/sol_augment/run.py"
    audit = "tools/independent_qa_audit.py"
    repair = "experiments/sol_augment/repair_medium.py"
    final = "tools/delivery_qa_gate.py"
    prompts = []

    def add(uid, title, relative, text, line, inputs, output, *, scope="current", assembly="", note=""):
        prompts.append(dict(id=uid, title=title, source=relative, line=line, text=text,
                            sha256=hashlib.sha256(text.encode()).hexdigest(), inputs=inputs,
                            output=output, scope=scope, assembly=assembly, note=note))

    relative = "scene_factory/screening/prompts/v4.txt"
    screening = (ROOT / relative).read_text()
    add("screen", "场景视觉筛选", relative, screening, 1,
        ["同一场景 8 张 RGB 图：四个方位，各 overview / closer", "场景 UID、试行数量/类别/区域门槛及取证预算", "拟保留时请求局部真实3D取证；来源名称不作为模型判断证据"],
        {"uid": "scene_001", "action": "inspect / decide", "decision": "keep / reject（仅decide）",
         "reason": "中文视觉理由", "object_count_lower_bound": "模型保守估计",
         "categories": "类别、数量下界与证据图", "regions": "区域及实际内容", "exploration_evidence": "换视角收益"},
        assembly="正文后附场景 UID 与可配置试行门槛。每张图附标签，图片使用 high detail。inspect 请求经K8s渲染后，真实新视图加入同一对话；默认最多2轮，每轮2图。",
        note="试行默认20个完整物件、8类、3区域，拟保留必须引用局部取证；这些数字是模型估计，不是GLB语义统计或人工验收。")

    plan, line = literal_assignment(run, "plan_prompt", function="run_scene")
    assembled = assignment_node(run, "prompt", "run_scene")
    add("plan-medium", "中等题：增强规划与出题", run, plan, line,
        ["原景八视角", "完整物件组联系图 groups.png", "可复制物件组 groups、可承载支撑面 supports、相机名 views"],
        {"items": [{"level": "medium", "target": "类别", "copies": [{"ids": [0], "support": 1, "at": [0.5, 0.5]}],
                    "question": "探索后才能回答的问题", "start": "视图名", "reveal": "视图名"}]},
        assembly=template(assembled.value, {"plan_prompt": "{{上述 prompt 正文}}"}),
        note="这一调用不输出答案；copies 描述物体放置。程序不会按 copies 数量计算语义答案。")

    answer, line = literal_assignment(run, "review_prompt", function="run_scene")
    add("answer-medium", "中等题：看图作答与自评", run, answer, line,
        ["实际增强成品的 start / reveal / region 图", "题目、档位、起始与揭示视图名"],
        {"items": [{"level": "medium", "answer": "图中观察到的答案", "pass": True, "reason": "实际证据"}]},
        assembly='正文后附 JSON questions；原代码字段前缀写作“\\n三题：”，但 focus=medium 实际只传一道题。',
        note="自评不是最终接纳；中等题产出后仍进入独立审核，即使自评为 false。")

    value, line = literal_assignment(audit, "MEDIUM_PROMPT")
    add("audit-medium", "独立中等题审核", audit, value, line,
        ["题目与候选模型答案", "起始图、揭示图、最多 2 张 region 图、overview 图"],
        {"uid": "scene_001", "pass": False, "bad_levels": ["medium"],
         "repair_action": "question / view / scene", "reason": "具体问题与返修要求"},
        assembly="正文后附场景 UID、题目和候选答案 JSON，再附带标签的图片。",
        note="独立指单独上下文的模型请求，默认仍使用同一模型；不是人工审核，也不是不同模型族交叉裁判。")

    value, line = literal_assignment(repair, "PROMPT")
    repair_owner = next(node for node in read_tree(repair).body if isinstance(node, ast.FunctionDef) and node.name == "repair")
    suffixes = []
    for node in ast.walk(repair_owner):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "run"
                and node.func.attr == "call" and len(node.args) > 1):
            suffixes.append(template(node.args[1], {"PROMPT": "{{上述返修公共正文}}"}))
    scene_prompt = assignment_node(repair, "prompt", "repair")
    assembly = "scene 分支：\n" + template(scene_prompt.value, {"PROMPT": "{{上述返修公共正文}}"})
    assembly += "\n\nquestion/view 分支与重新作答：\n" + "\n\n".join(suffixes)
    add("repair-medium", "中等题返修与重新作答", repair, value, line,
        ["上一版题目与答案", "独立审核的 repair_action 和 reason", "question/view 用已有成品图；scene 用原景八图、物件组与支撑面"],
        {"question": "修订题目", "start": "仅 view 分支选择已有相机", "reveal": "仅 view 分支选择已有相机",
         "answer_call": {"answer": "重新看图作答", "pass": True, "reason": "起始/遮挡证据"}},
        assembly=assembly,
        note="question 只修题；view 换已有机位；scene 重建中等场景。每次返修都会重新作答，再送独立审核。")

    value, line = final_review_prompt(final)
    add("delivery-review", "最终交付：自由相机可答性审核", final, value, line,
        ["题目、候选答案、initial_camera", "实际起始图、仅供审核的揭示图、overview 图"],
        {"pass": False, "observed_answer": "图中答案或无法确定", "reason": "具体原因",
         "repair_action": "none / question / view / reject"},
        assembly="先发送正文与图片，再以末尾文字块发送 question / answer / initial_camera JSON。",
        note="此环节由 production 批量编排启动；单独运行 auto / qa 不会自动包含这一额外关卡。最终关卡最多 3 次评审，仅自动返修 question/view，不再次重建场景。")

    extension = next(node for node in ast.walk(read_tree(final)) if isinstance(node, ast.AugAssign)
                     and isinstance(node.target, ast.Attribute) and node.target.attr == "PROMPT")
    add("delivery-repair-extension", "最终交付返修：追加约束", final, ast.literal_eval(extension.value), extension.lineno,
        ["追加到 repair_medium.PROMPT，仅在最终交付审核流程启用"],
        {"effect": "与中等题返修公共正文拼接，之后仍重新作答、审核"},
        note="这是最终交付返修使用的追加正文，不能误认为所有普通 qa 返修默认都会附加。")

    for uid, title, relative, name in (
        ("legacy-plan", "三档实验：增强规划", run, "PLAN_PROMPT"),
        ("legacy-answer", "三档实验：看图作答与自评", run, "REVIEW_PROMPT"),
        ("legacy-audit", "三档实验：独立审核", audit, "PROMPT"),
        ("legacy-question-revision", "三档实验：只修题目与答案", "experiments/sol_augment/revise_question.py", "PROMPT"),
    ):
        value, line = literal_assignment(relative, name)
        add(uid, title, relative, value, line, ["三档实验对应的场景图与题目"],
            {"levels": ["simple", "medium", "hard"]}, scope="experimental",
            note="保留的实验入口，当前 qa / auto / production 默认中等单题路径不使用此正文。")

    paths = {item["source"] for item in prompts} | {
        "scene_factory/sources.py", "scene_factory/automation.py", "scene_factory/screening/intake.py",
        "scene_factory/screening/judge.py", "tools/scene_feedback_loop.py", "tools/scene_production_batch.py"}
    files = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sorted(paths)}
    return dict(schema_version=1, generated_at=datetime.now(timezone.utc).isoformat(),
                repository="wzhwzhwzh0921/3D-Surfing", default_path="medium_only",
                source_files=files, prompts=prompts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    reference = export_reference()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(reference, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"prompt_count": len(reference["prompts"]), "output": str(args.out)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
