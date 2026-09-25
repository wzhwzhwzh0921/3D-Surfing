# Scene QA Factory / 3D 场景探索问答生成

给定一个可下载的 `.glb` URL，本仓库用**一次 Claude Code + DeepSeek Flash 会话**规划同一原场景的三次独立增强，并交付三套 `(scene.glb, question, model_answer, starting camera)`。题型与难度正交：定义 **10 类题 × 3 档难度**，单场景仍只出简单、中等、困难各一题。题型顺序由场景 ID 和种子可复现地随机产生，模型从可行题型里选三种。简单题起始证据不可见；中等题有近处明显遮挡；困难题先按线索找对地标，再绕开局部遮挡。具体 30 个组合见 [题型矩阵](docs/QUESTION_TYPES.md)。

当前输入索引 `catalog/qualified.jsonl` 含 **1,654 个**先前筛选的 GLB 地址（简单 1,240 / 复杂 414），只保存链接和许可等元数据，不把大型资产提交到仓库。每条记录的 `license` 保留原始许可，分发增强资产前需按原许可核查。

## 安装与配置

需要 Python 3.10+、Blender 5.x 和 Claude Code CLI。部署机器要有足够空间保存下载的原件和三份输出。凭据只通过环境变量或仓库外的 `--key-file` 传入，不会写入计划或产物；`api_usage.jsonl` 记录 token、cache 和重试统计，不含 API key。

```bash
git clone https://github.com/wzhwzhwzh0921/3D-Surfing.git
cd 3D-Surfing
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
# 在 .env 中填写 TOKENHUB_API_KEY 和本机 Blender 绝对路径
set -a; source .env; set +a
.venv/bin/sceneqa doctor
```

默认使用测试过的 TokenHub `https://tokenhub.tencentmaas.com/v1/chat/completions` 和 `deepseek/deepseek-flash`。仓库自带本地协议桥，把 Claude Code 的 Anthropic Messages 请求转成 TokenHub 的 OpenAI Chat Completions 请求，包含图片、工具调用、流式响应和缓存统计。也可使用官方 DeepSeek：`--provider deepseek`，并设置 `DEEPSEEK_API_KEY`；其 Anthropic 兼容根地址为 `https://api.deepseek.com/anthropic`，模型名为 `deepseek-flash`。两个网关均对 429/5xx 重试，单次响应输出上限设为 65,536 tokens，未设置轮次限制。网关的 cache 命中率依据上游 usage 字段计算；若上游未返回缓存字段，报告为未知。真实上游 key 只保留在本地网关进程，不传给可执行 Bash 的 Claude Code 子进程。单场景最多一个 Claude Code 会话；即便它的结果不通过最终验收，也只记录失败供续跑或人工处理。

## 单条与批量运行

```bash
sceneqa run --url 'https://example.org/scene.glb' --output-root /data/sceneqa-runs
sceneqa run --uid 0db322fa7a614975b83753a37c4e7350 --output-root /data/sceneqa-runs
sceneqa run --input-path /data/source.glb --output-root /data/sceneqa-runs
sceneqa batch --catalog catalog/qualified.jsonl --tier simple --offset 0 --limit 20 --workers 2 --output-root /data/sceneqa-runs
sceneqa list --tier complex --limit 10
sceneqa types --scene-id a450b90cb444431d93c6e4bab70fa233 --seed 0
```

`.env` 不会自动加载，必须先 `source`；若 key 存在私有文件中，可用 `--key-file /private/path/key.txt`，支持纯 key 文件或包含 `TOKENHUB_API_KEY=` / `TENCENT_ENDPOINT_API_KEY=` 的 `.env`。不要把私有文件放进本仓库。

`run` 支持 `--input-path` 本地 GLB 和 `--dry-run`，后者只做源资产预检和四张彩色预览，不调用模型。设置 `SCENEQA_QUESTION_SEED` 可改变可复现的题型顺序；模型可跳过不适合当前场景的题型，并在 `agent_notes.md` 说明原因。`batch` 默认一个 worker，最多四个，并按 `uid` 续跑：已有 `candidate`、`needs_review` 或 `visual_rejected` 结果会跳过；失败项保留 `status.json`、日志和中间产物。源下载有大小上限（默认 100 MB）、SHA256、GLB v2 头检查、断点式缓存与 429/5xx 重试。批量运行可用 `--max-bytes` 调整上限。

若原场景缺少至少两类可自然重复的小物件，或缺少困难题所需的两个可区分地标，agent 应生成 `unsuitable.json` 记录原因，批量结果标为 `unsuitable`，而不是复制房屋、横梁或地面凑题。目录筛选的“可下载、场景复杂度合适”不等同于“适合三道探索题”；这一判断也是正式入库前的人工复核项。

单场景的输出：

```text
runs/<uid>/
  source.glb                  原始资产
  inspect/inventory.json      网格名、包围盒和材质
  inspect/source_*.png        四个原始场景视角
  plan.json                   单次 agent 会话设计的三档计划
  agent_notes.md              模型对场景自然性的说明
  output/build_report.json    导出物件及起始/探测视角证据
  output/validation.json      模型答案、结构检查和通过/失败原因
  output/{simple,medium,hard}/scene.glb
  output/{simple,medium,hard}/start.png
  output/{simple,medium,hard}/reveal.png  揭示视角
  output/{simple,medium,hard}/detail.png  目标局部视角
  api_usage.jsonl             逐次 API 尝试和缓存统计
  status.json                 续跑状态
```

生成本地审查页：

```bash
sceneqa report --work /data/sceneqa-runs/<uid> --output /data/sceneqa-runs/<uid>/review.html
cd /data/sceneqa-runs/<uid> && python3 -m http.server 8000
```

打开 `http://localhost:8000/review.html`，可以旋转三档 GLB、查看起始截图与答案状态。网页通过 CDN 加载 `<model-viewer>`；离线环境仍可查看截图和下载模型。

## 质量边界

新版 `1.3` 要求三题类型不同，并验证起始遮挡、局部遮挡及困难题的最近地标线索。构建器还会输出每档 `reveal.png` 和 `detail.png`；程序检查 Claude Code 的工具日志，确认模型实际打开了六张图，随后由模型自行给出或修正三道题的答案。**程序不计算、不核对任何题目的答案**；`validation.json` 中的 `answer.source` 为 `model_visual`，`answer_verified` 为 `false`。复制对象只能来自原场景，并用支撑网格向下射线定位；导出保留原几何与材质。具体字段见 [计划协议](docs/PLAN_SCHEMA.md)。

`candidate` 表示三份模型和预设的结构/视角条件通过检查，**不表示模型答案正确或已认证为 benchmark gold**。程序无法仅凭 GLB 证明原场景同类物件清点完整、摆放符合语境，或一道题真的需要 30 次操作。若未来要发布严格题库，再对答案抽样复核并运行盲测 agent；当前流水线按你的要求保留模型自行作答的结果。

生成提示词还要求模型逐档查看 `start/reveal/detail.png` 并做语义自查：起始图及同款原物件是否泄露答案，揭示图是否清楚露出关键形状或颜色，题目指向的地标与方位是否唯一，以及复制件是否自然地落在场景里。发现捷径或歧义时，应修改题目、摆放或相机后重建，并在 `agent_notes.md` 记录每档的证据与残留疑点。这些是提示词要求，不是自动判真值；正式题库仍需人工审阅和盲测。

模型还需写 `visual_quality.json`，逐档声明视觉自查是否通过并说明原因。若任一档失败，即使 `validation.json` 为结构性 `candidate`，批处理的 `status.json` 也标为 `visual_rejected`；缺少自查文件则标为 `needs_review`。这只是保留模型自己的看图结论，并不自动核验答案。

若改用 Codex Sol 逐场景手工调用，使用[单场景任务模板](docs/CODEX_SOL_TASK.md)，每次替换源 GLB、工作目录和仓库路径；该模板包含相同的反捷径与视觉复核要求。

实际试跑和失败案例见 [验证记录](docs/SMOKE_TEST_2026-09-24.md)。旧版 `1.2` 已完成真实 GLB 的 DeepSeek 单会话交付；新版 `1.3` 的三档构建/校验和 DeepSeek 独立看图作答分别测过，但**新版全流程单会话尚未成功跑通**，不应按生产稳定性看待。

Codex Sol xhigh 的三个逐场景复核及本次提示词调整见 [2026-09-25 迭代记录](docs/ITERATIONS_2026-09-25.md)。其可视化审查页在工作区 `../scene-qa-runs/iterations-2026-09-25/index.html`；本轮的两个构建案例都未通过三档视觉复核，不能当作正式题库样本。

## 开发验证

```bash
python3 -m unittest discover -s tests -v
sceneqa build --source examples/fixtures/hidden.glb --plan examples/sample_plan.json --output /data/sceneqa-sample
sceneqa verify --plan examples/sample_plan.json --output /data/sceneqa-sample
```

`examples/fixtures/hidden.glb` 是仓库自制的 6 KB 遮挡示例，可用来验证三档构建与可见性门槛，不代表正式题库质量。

源码和索引可以独立发布；`runs/`、模型文件和 `.env` 默认不会进入 Git。授权内容和生成资产应存对象存储，再由审查页或题库索引引用。公开发布前还应确认使用的 GLB 许可允许对应再分发。
