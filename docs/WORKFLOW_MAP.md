# Pipeline 与代码地图

当前推荐主线是**程序自动联网找素材 → 下载 → 模型筛选 → 原景问答及审核 → 用户追加增难及审核 → 交付**。运行 `.venv/bin/python tools/start_followup_batch.py --out runs/online-200 --target 200` 即可从零启动，无需素材清单或种子原题；`auto` 是内部原题子流程。渲染默认本地，可显式选择 K8s；模型默认通过 Codex，可读取自己的 provider。完整命令见[README](../README.md)，接口分别见[模型](MODEL_INTERFACE.md)和[渲染](RENDERING.md)。

## 阶段、边界与产物

| 阶段 | 统一命令 | 实现 | 输入 → 输出 |
|---|---|---|---|
| 在线发现 | 主线内部 / `discover` / `auto` / `prepare` | `sources.py`、`live_sources.py`、`multi_sources.py`、`discovery.py` | 元数据 / 索引 → 可下载 URL、来源许可、UID |
| 下载与证据 | `screen` | `screening/intake.py` | URL / 本地 GLB → 文件哈希、八视角、相机 |
| 看图留删 | `screen` / `screen-images` | `screening/batch.py`、`rich.py` | 八视角 + 局部取景 → keep / reject / failed |
| 原题及审核返修 | `qa` | `tools/scene_feedback_loop.py` | 场景 → 中等题、模型答案、相机、审核、交付 |
| 二次增难 | `followup` | `followup.py` | `selected.jsonl` → bundle / chain + 作答及审核 |
| 自动补素材原题 | `auto` | `automation.py` | 来源策略 + 目标 → `selected.jsonl` |
| 完整主线：连续复合任务生产 | `followup-batch` | `followup_production.py` | 输出目录 + 目标（种子可省略）→ 自动找新素材、生成原题、完整任务及四个配额 |
| 多原题队列 | `production` | `tools/scene_production_batch.py` | `prepare` 分片 → 并行原题生产 + 最终审核 |
| 独立原题审核 | `review` / `delivery-review` | `tools/independent_qa_audit.py`、`delivery_qa_gate.py` | 题目 + 图片 + 候选答案 → 结构化审核 |
| 三档增强实验 | `augment` / `export` | `experiments/sol_augment/`、`tools/export_scene_delivery.py` | 原生物件复制计划 → simple / medium / hard，非默认复合任务出口 |

`auto/production` 的目标计数是原题交付；`followup-batch` 计数为整份复合任务，不能混用。原题审核失败不进入追加增难；处理异常不会被写成质量剔除。原资产不删除。

## 可替换的接口

```text
workflow CLI
  ├─ ModelBackend.complete(payload)        model_backend.py
  │    ├─ CodexBackend                    codex_backend.py
  │    │    ├─ ChatGPT 登录
  │    │    └─ 自己配置的 Codex provider
  │    └─ plugin 工厂                     自己的 Python 模块
  │  （relay 直接使用兼容 HTTP wire client）
  └─ RenderWorker.create/run/close         rendering.py
       ├─ LocalWorker                     本地子进程
       └─ KubernetesWorker                kubectl，明确 opt-in
```

素材发现默认检索扩展后的 ProSceneverse；可选 `multi-source` 轮流检索 ProSceneverse、ScanSceneverse，并从实际 Objaverse 索引解析 GLB，预置20组室内外检索，不依赖 Serper/Jina 或旧素材池。`discover` 可单独保存发现清单，不渲染或调用模型。“本地”是调度与渲染的位置，不表示离线生成。

模型与后端互不绑定；局部视角请求经 `screening/inspection.py` 串行调用相同渲染接口。GLB worker 可以单独上传，故导入前的执行位置检查保留在独立文件中。主调度模块不会导入 pyrender 或 bpy。

原题生成仍复用 `experiments/sol_augment/run.py`，其中 `Remote` 已是兼容工厂；不能把 experiments 目录整体删除。旧命令仍可用，但推荐统一入口以正确加载模型适配配置。

## Prompt 位置

| 用途 | 路径 |
|---|---|
| 当前模型筛选 | `scene_factory/screening/prompts/v5.txt`；v1–v4 用于历史复现 |
| 多目标 / 两跳需求 | `scene_factory/prompts/followup_plan_zh.txt` |
| 追加需求作答 | `scene_factory/prompts/followup_answer_zh.txt` |
| 追加需求审核 | `scene_factory/prompts/followup_audit_zh.txt` |
| 原题 / 三档实验规划与自评 | `experiments/sol_augment/run.py` 的 `PLAN_PROMPT`、`REVIEW_PROMPT`、`focus == 'medium'` 分支 |
| 场景 / 视角返修 | `experiments/sol_augment/repair_medium.py` |
| 只修问答 | `experiments/sol_augment/revise_question.py` |
| 独立原题审核 | `tools/independent_qa_audit.py` 的 `PROMPT` / `MEDIUM_PROMPT` |
| 最终交付审核 | `tools/delivery_qa_gate.py` |

本次整理保留这些语义规则，不重新定义难度或把模型审核变成程序化答案检查。

## 配置、批次与交付

`.env.example` 列出公开配置，私有凭据只在自己的环境 / Codex 目录。`workflow config` 只读展示模型名、渲染配置和素材来源，不登录、不请求模型、不取景。后台启动器为新批次冻结 Python 与 Prompt；`runtime/` 不含 `.env`、原资产或模型凭据。

`summary.json` 是当前计数与阶段；`registry.jsonl` 是任务状态；`keep/reject/failed.jsonl` 分开记录成功、质量拒绝、执行失败；`delivery-manifest.jsonl` 指向审核通过的任务。`tasks/<uid>/<mode>/delivery` 包含 `scene.glb`、`start.png`、`task.json`、`answers.json`、`audit.json`。`originals/` 保留在线来源、筛选及原题闭环。

缓存绑定模型、图片、Prompt、渲染配置和代码；跨版本用新目录。旧批次继续原冻结版本，不能用清除指纹的办法迁移。`catalog/qualified.jsonl` 只是历史来源索引，不是当前验收清单。公开网站是历史导出快照，不会因仓库配置变化而自动更新。
