# 带操作轨迹的 3D 盲测

`candidate` 与静态图监督只说明预设证据存在，不能证明真实探索路径。`blind-review` 为单题生成一个只显示题目和 GLB 的本地网页；页面没有生成器答案、建模计划、网格名称或补充截图。审核者通过 `focus_at(x,y)`、`orbit(yaw,pitch)`、`zoom(factor)`、`move(direction,distance)`、`undo_view()` 导航，最后记录“看清后作答 / 猜测 / 无法解答”并导出轨迹 JSON。按钮里的数值可改，点击模型表面也能换焦；没有一键重置。

```bash
sceneqa blind-review --work /data/runs/scene-id --level medium \
  --output /data/runs/scene-id/blind-medium.html
cd /data/runs && python3 -m http.server 8000
```

打开 `http://localhost:8000/scene-id/blind-medium.html`。页面使用固定版本的 `<model-viewer>`，需要能访问 CDN；GLB 仍由本地 HTTP 服务提供。每次成功操作生成新的 `observation_id`，旧 ID 会被拒绝；失败的拾取或越界参数不会写入轨迹。`focus_at` 把当前 2D 点击位置投到**可见表面**，再把相机目标移到对应 3D 点；无命中则返回 `no_surface_at_point`。浏览器还暴露 `window.sceneqaAgent.snapshot()` 和 `window.sceneqaAgent.act(tool,args)`，供受限的 Playwright 代理封装。给模型接入时只应提供截图和这两个受控操作，不能开放任意 DOM、JavaScript 或 GLB 元数据读取。

下载的 JSON 绑定题干、GLB SHA256、档位及起始机位，记录每步参数、前后观察 ID、相机姿态和最终独立答案。不要把“操作 30 次”直接当作难题：一次换焦可能已经露出答案；多次无效旋转也不会提高题目质量。应先确认答案正确且状态为“看清证据”，再分析操作分布和必要的因果步骤。

```bash
sceneqa blind-summarize --traces /data/blind-traces --runs-root /data/runs \
  --output /data/blind-summary.json
sceneqa blind-gallery --traces /data/blind-traces --runs-root /data/runs \
  --output /data/blind-runs/challenges.html
```

摘要会读取目录第一层的手动导出 JSON，以及子目录中 `blind-run` 完成后生成的 `blind-trace.json`；超时/停滞的 `partial-trace.json` 不计入有效试验。它排除格式错误及与当前题目、机位、GLB 不匹配的旧轨迹，按档位报告有效次数、观察后作答/猜测/未解比例、观察后作答的操作数中位数与 P90、零操作作答数，以及各工具调用次数。`challenge_flags` 会列出“监督通过但盲解未解/猜测/零操作作答”“独立答案与生成器答案不同”；即使没有监督报告，盲解未解、猜测或零操作就作答也会列为较弱的待复核线索。`blind-gallery` 将这些有效冲突逐题呈现，含当前 GLB、Blender 起始图、盲解动作和现存的逐帧截图，并提示应核对机位、目标可达性、题干或答案。生成页面与 GLB/截图需要在同一个本地 HTTP 服务可访问的路径下。这些都是**待复核线索，不是自动判错或答案准确率**；同一资产的重复试验会单独显示 `unique_assets`。要判断题目是否真的达到三档标准，需要由另一名审核者核对独立答案和轨迹中的决定性证据。

此页使用 `modelviewer_perspective_v1`：GLB 的起始方向映射自 Blender `start_camera`，但浏览器是透视投影，工厂起始 PNG 是正交投影，画幅和遮挡可能不同。因而盲测轨迹可以校准**浏览器导航协议**的难度，不能直接代替对 Blender 原始 `start.png` 的泄漏审核，也不能与其他渲染协议的操作数混算。官方 API 参考：[相机轨道与目标](https://modelviewer.dev/examples/stagingandcameras/)、[屏幕点拾取](https://modelviewer.dev/docs/#entrydocs-annotations-methods-positionAndNormalFromPoint)。

本地 Chromium 冒烟已在 Low Poly House GLB 上验证加载、换焦、旋转、缩放、平移、退回上一视角、旧观察 ID 拒绝、空白拾取拒绝及导出/汇总 JSON。这只证明交互和记录路径能运行；没有真人或模型完成并核对题目答案。

## 让模型自主盲解

仓库提供同一浏览器协议的 `blind-run`。它给模型的只有题干、当前 RGB 截图、观察 ID、回退可用次数，以及五个相机工具和 `submit_answer`；浏览器桥不接受任意 JavaScript、DOM 查询或文件读取。模型每轮最多执行一个工具，成功操作后收到新的截图。没有固定轮数上限，只有 `--max-seconds` 截止和连续八次无有效操作的停滞保护。

首次在该环境运行需安装 Node 侧 Playwright，并准备 Chromium：

```bash
npm ci --prefix scene_factory/static/blind-agent
cd scene_factory/static/blind-agent && npx playwright install chromium && cd ../../..
```

然后使用 TokenHub 或兼容的 Chat Completions 接口；密钥可放在已有的 `TOKENHUB_API_KEY` 环境变量或仅本机可读的 `--key-file` 中：

```bash
sceneqa blind-run --work /data/runs/scene-id --level medium \
  --output /data/blind-runs/scene-id-medium \
  --model deepseek/deepseek-flash --key-file /secure/tokenhub.env \
  --max-seconds 900
```

每次请求的 `max_completion_tokens` 为 65536；429/5xx 有重试，`usage.jsonl` 和 `summary.json` 保留模型用量、缓存命中与重试次数。`observations/` 是模型实际收到的逐帧截图，`actions.jsonl` 包含成功及失败的工具调用；只有模型通过 `submit_answer` 结束时才生成可供 `blind-summarize` 接收的 `blind-trace.json`。截止或停滞留下 `partial-trace.json` 供诊断，不会被算成“无法解答”的有效试验。`summary.json` 中 `accuracy_judged=false`，需要独立核对答案。

本地假模型接口的端到端冒烟包含一次 429、两次成功 API 回复、一轮旋转和一次提交：浏览器产生了两张截图，模型请求保留 64k 上限；用量汇总记录 `cache_hit_rate=0.60`，生成的轨迹能通过 `blind-summarize`。这仅验证协议、重试、缓存统计与文件流，**尚无真实 DeepSeek/GLM 盲解结果**。
