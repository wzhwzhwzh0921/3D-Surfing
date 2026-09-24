# Scene QA Factory / 3D 场景探索问答生成

给定一个可下载的 `.glb` URL，本仓库用**一次 Claude Code + DeepSeek V4.1 Flash 会话**规划同一原场景的三次独立增强，并交付三套 `(scene.glb, question, programmatic answer, starting camera)`。三档分别为简单、中等、困难，目标物件类别、题目和答案必须不同。模型选择场景内原有物件作模板，程序复制原生几何与材质、导出三份 GLB，并做起始视角遮挡、24 个全局视角加必要的局部换焦视角可发现性、重复答案、碰撞等检查。新版 `1.1` 的中档最多新增两个目标，必须用场景中的近处道具形成可测量的遮挡；困难档至少需要两个不同的局部遮挡点，不靠堆数量或只靠外围墙制造难度。五类题目及其答案校验边界见 [题型设计](docs/QUESTION_TYPES.md)。

当前输入索引 `catalog/qualified.jsonl` 含 **1,654 个**先前筛选的 GLB 地址（简单 1,240 / 复杂 414），只保存链接和许可等元数据，不把大型资产提交到仓库。每条记录的 `license` 保留原始许可，分发增强资产前需按原许可核查。

## 安装与配置

需要 Python 3.10+、Blender 5.x 和 Claude Code CLI。部署机器要有足够空间保存下载的原件和三份输出。当前开发机系统盘空间很少，演示时可把 `--output-root` 指到 `/dev/shm` 或另一块磁盘。凭据只通过环境变量传入，不会写入计划或产物；`api_usage.jsonl` 记录 token、cache 和重试统计，不含 API key。

```bash
cd /home/ubuntu/blender/scene-qa-factory
python3 -m venv .venv
.venv/bin/pip install -e .
export TOKENHUB_API_KEY='从密钥管理器注入'
export SCENEQA_BLENDER='/path/to/blender'
.venv/bin/sceneqa doctor
```

默认使用先前测试的 TokenHub `https://tokenhub.tencentmaas.com/v1/chat/completions` 和 `deepseek/deepseek-flash`。仓库自带本地协议桥，把 Claude Code 的 Anthropic Messages 请求转成 TokenHub 的 OpenAI Chat Completions 请求，包含图片、工具调用、流式响应和缓存统计。也可使用官方 DeepSeek：`--provider deepseek`，并设置 `DEEPSEEK_API_KEY`；其 Anthropic 兼容根地址为 `https://api.deepseek.com/anthropic`，V4.1 Flash 模型名为 `deepseek-flash`。两个网关均对 429/5xx 重试，单次响应输出上限设为 65,536 tokens，未设置轮次限制。网关的 cache 命中率依据上游 usage 字段计算；若上游未返回缓存字段，报告为未知。真实上游 key 只保留在本地网关进程，不传给可执行 Bash 的 Claude Code 子进程。单场景最多一个 Claude Code 会话；即便它的结果不通过最终验收，也只记录失败供续跑或人工处理。

## 单条与批量运行

```bash
sceneqa run --url 'https://example.org/scene.glb' --output-root /data/sceneqa-runs
sceneqa run --uid 0db322fa7a614975b83753a37c4e7350 --output-root /data/sceneqa-runs
sceneqa batch --catalog catalog/qualified.jsonl --tier simple --offset 0 --limit 20 --workers 2 --output-root /data/sceneqa-runs
sceneqa list --tier complex --limit 10
```

若 key 存在私有文件中，使用 `--key-file /private/path/key.txt`；支持纯 key 文件或包含 `TOKENHUB_API_KEY=` / `TENCENT_ENDPOINT_API_KEY=` 的 `.env`。不要把私有文件放进本仓库。

`run` 支持 `--input-path` 本地 GLB 和 `--dry-run`，后者只做源资产预检和四张彩色预览，不调用模型。`batch` 默认一个 worker，最多四个，并按 `uid` 续跑：已有通过验收的候选集会跳过；失败项保留 `status.json`、日志和中间产物。源下载有大小上限（默认 100 MB）、SHA256、GLB v2 头检查、断点式缓存与 429/5xx 重试。批量运行可用 `--max-bytes` 调整上限。

若原场景不足三种**不同且可自然复制**的小物件，agent 应生成 `unsuitable.json` 记录原因，批量结果标为 `unsuitable`，而不是复制房屋、横梁或地面凑题。目录筛选的“可下载、场景复杂度合适”不等同于“适合三道探索题”；这一判断也是正式入库前的人工复核项。

单场景的输出：

```text
runs/<uid>/
  source.glb                  原始资产
  inspect/inventory.json      网格名、包围盒和材质
  inspect/source_*.png        四个原始场景视角
  plan.json                   单次 agent 会话设计的三档计划
  agent_notes.md              模型对场景自然性的说明
  output/build_report.json    导出物件及起始/探测视角证据
  output/validation.json      程序答案和通过/失败原因
  output/{simple,medium,hard}/scene.glb
  output/{simple,medium,hard}/start.png
  api_usage.jsonl             逐次 API 尝试和缓存统计
  status.json                 续跑状态
```

生成本地审查页：

```bash
sceneqa report --work /data/sceneqa-runs/<uid> --output /data/sceneqa-runs/<uid>/review.html
cd /data/sceneqa-runs/<uid> && python3 -m http.server 8000
```

打开 `http://localhost:8000/review.html`，可以旋转三档 GLB、查看起始截图与程序答案。网页通过 CDN 加载 `<model-viewer>`；离线环境仍可查看截图和下载模型。

## 质量边界

候选集必须通过结构化计划、三份有效且不同的 GLB、可计算且互异的答案、非直接揭晓的起始视角、24 视角或局部换焦可发现性以及复制件碰撞检查。`1.1` 中档至少有一处、困难档至少有两处不同的近处遮挡：构建器以目标表面采样射线验证至少 35% 从起始视角被指定 mesh 挡住。复制对象只能来自原场景，并用支撑网格向下射线定位；导出保留原几何与材质。具体字段见 [计划协议](docs/PLAN_SCHEMA.md)。

`candidate` **不是已认证的 benchmark gold**。程序无法仅凭 GLB 证明原场景同类物件已经清点完整、摆放符合语境，或一道题真的需要 30 次操作。`validation.json` 会列出模型物件类别与原网格名不匹配的语义风险，以及构建器对轻微重叠所做的坐标调整。发布正式题库前，应增加人工复核和盲测 agent 的探索轨迹，并把审核结果写到外部标注表。当前协议聚焦数量/条件数量/比较题，暂不自动生成「沙发背面毯子颜色」这类材质题；那需要独立的可验证颜色标注。

实际试跑和失败案例见 [验证记录](docs/SMOKE_TEST_2026-09-24.md)。

## 开发验证

```bash
python3 -m unittest discover -s tests -v
sceneqa build --source examples/fixtures/hidden.glb --plan examples/sample_plan.json --output /data/sceneqa-sample
sceneqa verify --plan examples/sample_plan.json --output /data/sceneqa-sample
```

`examples/fixtures/hidden.glb` 是仓库自制的 6 KB 遮挡示例，可用来验证三档构建与可见性门槛，不代表正式题库质量。

源码和索引可以独立发布；`runs/`、模型文件和 `.env` 默认不会进入 Git。授权内容和生成资产应存对象存储，再由审查页或题库索引引用。公开发布前还应确认使用的 GLB 许可允许对应再分发。
