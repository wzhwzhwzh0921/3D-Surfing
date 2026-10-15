# 工作流脚本

默认完整造题命令：`.venv/bin/python tools/start_followup_batch.py --out runs/online-200 --target 200`。程序自行联网检索、下载和筛选场景，生成原题、答案及追加增难任务；无需预先提供 URL、GLB 或种子原题。默认本地渲染，K8s 可选，模型按环境配置接入。

各阶段调试时，从仓库根目录使用 `sceneqa workflow list` 和 `sceneqa workflow <阶段> --help`。
完整依赖关系见[流程与代码地图](../docs/WORKFLOW_MAP.md)。

| 文件 | 统一命令 | 职责 |
|---|---|---|
| `start_followup_batch.py` | 后台启动 `workflow followup-batch` | **完整主线**：自动找新素材、原题、二次增难、审核及交付 |
| `../scene_factory/followup.py` | `workflow followup` | 对已经审核的原题单独追加需求，不主动发现新素材 |
| `scene_pipeline.py` | `workflow screen` | 筛选兼容入口，实际实现为 `scene_factory.screening.intake` |
| `scene_screen_batch.py` | `workflow screen-images` | 图片筛选兼容入口，实际实现为 `scene_factory.screening.batch` |
| `scene_feedback_loop.py` | `workflow qa` | 增强、出题、中等题审核与返修 |
| `scene_auto_pipeline.py` | `workflow auto` | 发现素材并顺序生产 |
| `prepare_batch_sources.py` | `workflow prepare` | 元数据预筛、类别平衡及来源分片 |
| `scene_production_batch.py` | `workflow production` | 多队列调度、最终审核与交付汇总 |
| `independent_qa_audit.py` | `workflow review` | 独立模型审核已有题目和图片证据 |
| `delivery_qa_gate.py` | `workflow delivery-review` | 生产期间持续审核唯一指代、遮挡及答案证据 |
| `export_scene_delivery.py` | `workflow export` | 导出三档增强实验，不是当前中等题生产的交付出口 |

筛选旧命令的参数保持兼容；如果通过 Python 导入筛选实现，请使用
`scene_factory.screening.intake`、`judge`、`batch` 等模块，不要导入这两个入口脚本。
