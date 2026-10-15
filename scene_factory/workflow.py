"""Unified access to the current repository workflows, with lazy stage imports."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import importlib
import json
import os
from pathlib import Path
import runpy
import sys


@dataclass(frozen=True)
class Stage:
    description: str
    implementation: str
    module: str | None = None


# Keep orchestration in its existing files until those stages are redesigned.
# Only screening has been moved into the package in this cleanup.
STAGES = {
    "discover": Stage("只发现在线 GLB 来源、保存来源证据及续跑游标；不渲染、不调用模型",
                      "scene_factory/discovery.py", "scene_factory.discovery"),
    "smoke": Stage("使用仓库小样验证渲染、原景保留和局部相机；不调用模型",
                   "scene_factory/smoke.py", "scene_factory.smoke"),
    "screen": Stage("准备 GLB、取景、筛选，输出留/删/失败清单",
                    "scene_factory/screening/intake.py", "scene_factory.screening.intake"),
    "screen-images": Stage("复用八视角筛选，拟保留场景追加3D取证",
                           "scene_factory/screening/batch.py", "scene_factory.screening.batch"),
    "qa": Stage("增强、出题、独立审核与返修闭环", "tools/scene_feedback_loop.py"),
    "followup": Stage("保留原题与起始视角，追加多目标或空间推理需求并独立审核",
                      "scene_factory/followup.py", "scene_factory.followup"),
    "followup-batch": Stage("在线补充原题，持续追加用户需求，达到审核通过数量后停止",
                            "scene_factory/followup_production.py", "scene_factory.followup_production"),
    "auto": Stage("在线发现素材并顺序筛选、出题，达到交付目标后停止", "tools/scene_auto_pipeline.py"),
    "prepare": Stage("准备下载来源并均衡分片，场景质量交给模型筛选", "tools/prepare_batch_sources.py"),
    "production": Stage("调度多个生产队列和最终审核", "tools/scene_production_batch.py"),
    "review": Stage("独立模型审核已有问答证据", "tools/independent_qa_audit.py"),
    "delivery-review": Stage("最终自由相机题目审核与返修", "tools/delivery_qa_gate.py"),
    "export": Stage("导出三档增强实验产物；生产交付由 production 汇总", "tools/export_scene_delivery.py"),
    "augment": Stage("原景检查或三档增强，生产闭环使用的实现",
                     "experiments/sol_augment/run.py"),
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="sceneqa workflow", description=__doc__,
                                     epilog="运行 sceneqa workflow <阶段> --help 查看原有参数。")
    commands = parser.add_subparsers(dest="stage")
    listing = commands.add_parser("list", help="列出阶段及实现位置，不执行任务")
    listing.add_argument("--json", action="store_true")
    commands.add_parser("config", help="显示当前模型和渲染配置；不调用模型或渲染")
    for name, stage in STAGES.items():
        commands.add_parser(name, help=stage.description, add_help=False)
    if not argv:
        parser.print_help()
        return 0
    if argv[0] not in STAGES:
        args = parser.parse_args(argv)
        if args.stage == "config":
            from .model_backend import configured_model
            from .rendering import render_settings
            from .sources import DEFAULT_PROVIDER
            print(json.dumps({"model": {"backend": os.getenv("SCENEQA_MODEL_BACKEND", "codex"),
                              "name": configured_model(), "codex_auth": os.getenv("SCENEQA_CODEX_AUTH", "chatgpt"),
                              "codex_provider": os.getenv("SCENEQA_CODEX_PROVIDER", "default")},
                              "render": render_settings(),
                              "source_provider": os.getenv("SCENEQA_SOURCE_PROVIDER", DEFAULT_PROVIDER)}, ensure_ascii=False, indent=2))
            return 0
        if args.stage == "list":
            if args.json:
                print(json.dumps({name: asdict(stage) for name, stage in STAGES.items()},
                                 ensure_ascii=False, indent=2))
            else:
                for name, stage in STAGES.items():
                    print(f"{name:16} {stage.description}\n{'':16} {stage.implementation}")
        return 0
    if "--help" not in argv and "-h" not in argv:
        from .codex_backend import MODEL_STAGES, launch
        backend = os.getenv("SCENEQA_MODEL_BACKEND", "codex")
        if backend not in {"codex", "relay", "plugin"}:
            parser.error("SCENEQA_MODEL_BACKEND must be codex, plugin or relay")
        if argv[0] in MODEL_STAGES and backend in {"codex", "plugin"} and not os.getenv("SCENEQA_CODEX_ACTIVE"):
            return launch(argv)
        if argv[0] in MODEL_STAGES and backend == "relay":
            from .model_backend import configure_relay, configured_model
            for index, value in enumerate(argv):
                if value == "--model" and index + 1 < len(argv):
                    os.environ["SCENEQA_MODEL"] = argv[index + 1]
                elif value.startswith("--model="):
                    os.environ["SCENEQA_MODEL"] = value.split("=", 1)[1]
            configure_relay()
            if argv[0] in {"auto", "screen", "screen-images", "production", "review", "delivery-review"}:
                argv += ["--url", os.environ["LUNA_CHAT_URL"], "--model", configured_model()]
    stage = STAGES[argv[0]]
    if stage.module:
        result = importlib.import_module(stage.module).main(argv[1:])
        return result or 0

    path = Path(__file__).resolve().parents[1] / stage.implementation
    if not path.is_file():
        parser.error("该阶段需要完整源码仓库。请在仓库执行 pip install -e '.[batch]'；"
                     f"找不到 {path}")
    previous_argv = sys.argv
    previous_path = sys.path[:]
    try:
        sys.argv = [str(path), *argv[1:]]
        runpy.run_path(str(path), run_name="__main__")
    finally:
        sys.argv = previous_argv
        sys.path[:] = previous_path
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
