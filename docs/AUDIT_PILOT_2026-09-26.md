# 场景审核首轮抽样与预检（2026-09-26）

用 `sceneqa audit-sample --per-tier 10 --seed 260926` 从 `catalog/qualified.jsonl` 的 1,654 条记录中抽取 [20 个候选场景](../catalog/audit-pilot-2026-09-26.jsonl)。简单/复杂各 10 个；每档明确室内 4、明确室外 4、分类待判 2。单一分类最多 2 个，同一作者只取 1 个；排除元数据体积超过 100 MB 的资产。这个抽样方案用于**审核流程试验**，不代表最终题库分布。

[HTTP HEAD 记录](../catalog/audit-pilot-2026-09-26-head-check.json)显示 20 个 URL 当时均返回 200，响应长度均与目录字节数一致。随后对全部 20 个场景执行 `sceneqa batch --dry-run --workers 3`：20 个都成功下载、通过 SHA256 检查，并由 Blender 生成库存和四个彩色预览。运行结果保存在本机 `/home/ubuntu/blender/scene-qa-runs/audit-pilot-preflight-batch.json`；[本地预览页](/home/ubuntu/blender/scene-qa-runs/audit-pilot-preflight/index.html)展示所有四视角图。预检不调用 DeepSeek，也不生成题目。

基于库存名称和网格数量的粗筛有 7 个 `likely_unsuitable`、13 个 `needs_agent_review`。其中 6 个缺少至少两个可独立复制的小网格；另 1 个只是物件名称类别少，仍有 14 个小网格。本轮修正了准入逻辑：**只有前一种几何缺失才自动拒收**，名称不丰富时交给 agent 看图判断。`likely_unsuitable` 仍不是视觉质量真值，合并网格可能把画面丰富的场景压成极少对象。一个已目视查看的 `Room Office - Diorama` 虽然落在 `needs_agent_review`，实景仍比较稀疏，说明 13 个也不能直接计作可用题源。另一个港口场景虽然可载入，但主要是重复集装箱，需要人工判断能否产生三道不同、自然且有视角难度的问题。

本次**没有**得到 60 道题、人工 gold、DeepSeek 生成成功率或监督者准确率。下一步应在预览页逐个标记可用性；若要维持 20 个有效场景，补抽候选直至有足够通过人工初筛的来源，再运行生成和逐题监督试验。`audit-sample` 的种子可变，补抽时要排除已看过的场景以免把同一来源重复计入独立测试集。
