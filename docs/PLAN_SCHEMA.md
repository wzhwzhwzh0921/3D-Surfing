# 单场景三档增强计划格式

新运行使用 `schema_version: "1.3"`。一个原始 GLB 生成简单、中等、困难三个**独立**版本，每档一种不同的 `question_type`，类型须来自 [10 × 3 题型表](QUESTION_TYPES.md)。`1.3` 中所有答案均由模型查看构建后的图片后自行填写；程序不计算或核对答案。旧版 `1.0/1.1/1.2` 仍可读取，用来复核历史产物。字段参考见 [自制小场景结构样例](../examples/model_answer_fixture_plan.json)；[花园回归样例](../examples/model_answer_garden_plan.json)仅用于构建测试。样例里的答案不是通用真值，不能直接复用到新场景。

新批处理先按[候选题与机位探测协议](CANDIDATE_PROBE.md)写六道候选、每档两道，再从探测通过的候选中选三道。最终计划增加 `workflow_version: "candidate_probe_v1"`，每档保留 `candidate_id`、`start_landmarks` 和探测出的 `start_camera`。改变候选摆放或题干后必须重新探测。

```json
{
  "schema_version": "1.3",
  "scene_id": "example",
  "variants": [
    {
      "level": "simple",
      "entities": [{"id": "coin_a", "kind": "coin", "objects": ["CoinMesh"], "region": "lawn"}],
      "copies": [{"id": "coin_hidden", "from": "coin_a", "xy": [0.3, 1.2], "support_object": "Ground", "region": "lawn"}],
      "question": {"question_type": "count", "mode": "count", "kind": "coin", "text": "草坪上有几枚硬币？", "region": "lawn", "declared_answer": 2, "evidence_ids": ["coin_a", "coin_hidden"]},
      "start_camera": {"azimuth_deg": 20, "elevation_deg": 30}
    },
    {
      "level": "medium",
      "entities": [], "copies": [], "question": {}, "start_camera": {},
      "occlusion_targets": [{"target_copy_id": "复制件 ID", "blocker_objects": ["近处遮挡物的原始 mesh 名"]}]
    },
    {
      "level": "hard",
      "entities": [], "copies": [], "question": {}, "start_camera": {},
      "occlusion_targets": [{"target_copy_id": "答案复制件 ID", "blocker_objects": ["MushroomNearCarrot"]}],
      "anchor_query": {
        "relation": "nearest", "reference_object": "CarrotMesh",
        "candidate_objects": ["MushroomNearCarrot", "MushroomNearGate"],
        "answer_anchor_object": "MushroomNearCarrot", "target_copy_id": "答案复制件 ID"
      }
    }
  ]
}
```

中、困难档的空 `entities/copies/question` 只是字段示意，不能直接运行。对象名必须来自 `inspect/inventory.json`。`entities` 要穷举与题目有关的原始实例；`copies` 从它们复制并贴合指定支撑面。原始实例是否清点完整仍需人工复核。

`question` 的答案模式：

| 题型 | `mode` | 必要字段 | 程序处理 |
|---|---|---|---|
| `count` | `count` 或 `compare` | `kind`、`declared_answer`、`evidence_ids`，可选 `pose/region`，比较时需 `other_kind` | 保存模型答案 |
| `identity` | `identify` | `kind`、`answer_copy_id`、`anchor_blocker_object`、`declared_answer`、`evidence_ids` | 保存模型答案 |
| 其他八类 | `evidence` | `kind`、`declared_answer`、`evidence_ids` | 保存模型答案 |

`question.evidence_ids` 应列出所有必须同框的原始实体和复制件；可再用 `question.evidence_context_objects` 列出区域边界、床头柜、方向地标等原始 mesh 名。构建器从这些物件生成 `answer_view.png`，同时报告 `answer_view_coverage`；`candidate_probe_v1` 要求这些证据组在该视角都可见。这个门槛只检查可见性，不判断答案内容。

**难度与题型正交：** 三档均可选择 10 类中可行的一类；相同类型可在不同场景出不同难度。简单题的答案证据在起始视角不可见。中等题最多新增两个目标，至少一个必须被近处自然物体挡住关键证据。困难题先由 `anchor_query` 在至少两个候选地标中唯一选出最近的地标，再去该地标绕开局部遮挡寻找答案。困难档的第二个遮挡目标/干扰项可以增加辨别力，但不是硬性要求。新版候选探测用目标表面射线检测遮挡：至少 10 条有效射线，指定物件阻挡不少于 20%，且占全部首次遮挡命中的至少一半；遮挡物水平跨度不超过场景 45%，且距目标不能过远。这个数值只是准入下限，模型仍须在图中确认关键证据确实被挡住；仅边缘被遮住但答案已暴露不合格。旧版计划复核继续使用原来的 35% 门槛。`anchor_query` 计算候选 mesh 中心的世界 XY 距离，最近与次近至少相差次近距离的 15%。

每次构建会生成 `start.png`、`reveal.png`（能看到最多复制件的全景视角）、`detail.png`（一个目标复制件的局部视角）和 `answer_view.png`（按题目证据组共同取景）。模型须查看三档的四类图，再填写或修正 `declared_answer`。`evidence_ids` 必须指向计划中的原始实体或复制件。校验器只检查图片文件、GLB、复制件、起始遮挡和困难题地标关系；通过后状态为 `candidate`，但 `answer_verified: false`。人工 gold 仍需另行确认自然性、颜色纹理语义、答案和盲测轨迹。视角覆盖不等于「至少 30 次工具调用」。
