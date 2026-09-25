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
```

摘要会排除格式错误及与当前题目、机位、GLB 不匹配的旧轨迹，按档位报告有效次数、观察后作答/猜测/未解比例、观察后作答的操作数中位数与 P90、零操作作答数，以及各工具调用次数。**这些仍不是答案准确率或独立的场景数**；同一资产的重复试验会单独显示 `unique_assets`。要判断题目是否真的达到三档标准，需要由另一名审核者核对独立答案和轨迹中的决定性证据。

此页使用 `modelviewer_perspective_v1`：GLB 的起始方向映射自 Blender `start_camera`，但浏览器是透视投影，工厂起始 PNG 是正交投影，画幅和遮挡可能不同。因而盲测轨迹可以校准**浏览器导航协议**的难度，不能直接代替对 Blender 原始 `start.png` 的泄漏审核，也不能与其他渲染协议的操作数混算。官方 API 参考：[相机轨道与目标](https://modelviewer.dev/examples/stagingandcameras/)、[屏幕点拾取](https://modelviewer.dev/docs/#entrydocs-annotations-methods-positionAndNormalFromPoint)。

本地 Chromium 冒烟已在 Low Poly House GLB 上验证加载、换焦、旋转、缩放、平移、退回上一视角、旧观察 ID 拒绝、空白拾取拒绝及导出/汇总 JSON。这只证明交互和记录路径能运行；没有真人或模型完成并核对题目答案。
