# 监督者返修循环验证记录（2026-09-26）

已验证的程序行为：

- 本地 `sceneqa run --input-path examples/fixtures/hidden.glb --output-root /dev/shm/sceneqa_supervisor_smoke --supervisor-rounds 1 --dry-run` 完成源资产检查，并生成包含独立监督提示的任务文件；未调用模型。
- 测试模拟了监督者阅读全部 12 张图、只读权限、独立模型路由、定点返修、复审通过、复审无改善回滚、误改已通过档位回滚、误改源 GLB 回滚，以及备份失败时保留原产物。
- `sceneqa supervise --work .../nina_interior --repairs 0` 在调用模型前准确拒绝缺少三张 `answer_view.png` 的旧产物，避免用不完整证据作监督结论。
- 将 Low Poly House 的现有 `plan.json`、源 GLB 与三份完整输出组合成只读审核试件后，`sceneqa supervise --repairs 0` 通过结构及 12 图准入检查，随后明确停在 `No tokenhub key`；这只证明已有产物的监督入口可到达模型调用前，不等于模型已经给出审核结果。
- `report` 页面只显示与当前三份 GLB 及十二张证据图哈希一致的监督报告；旧尝试回滚或重渲染后不会误贴新版意见。

尚未验证：真实 DeepSeek/GLM 审核的准确率、模型是否总能按 JSON 约定输出、返修后视觉质量是否提高，以及 API 成本/缓存命中率。当前运行环境的 `sceneqa doctor` 显示 `api_key_configured: false`，因此没有将模拟测试描述成端到端模型测试。正式启用前先抽 20 个不同类型场景，按每档人工标注 `pass/review/fail`，报告逐题混淆矩阵、误收率、返修净提升、平均耗时与用量。
