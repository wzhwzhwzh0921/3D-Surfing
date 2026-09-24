# 单场景三档增强计划格式

新运行使用 `schema_version: "1.2"`。一个原始 GLB 生成简单、中等、困难三个**独立**版本，每档一种不同的 `question_type`，类型须来自 [10 × 3 题型表](QUESTION_TYPES.md)。旧版 `1.0/1.1` 仍可读取，用来复核历史产物。

```json
{
  "schema_version": "1.2",
  "scene_id": "example",
  "variants": [
    {
      "level": "simple",
      "entities": [{"id": "coin_a", "kind": "coin", "objects": ["CoinMesh"], "region": "lawn"}],
      "copies": [{"id": "coin_hidden", "from": "coin_a", "xy": [0.3, 1.2], "support_object": "Ground", "region": "lawn"}],
      "question": {"question_type": "count", "mode": "count", "kind": "coin", "text": "草坪上有几枚硬币？", "region": "lawn"},
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
| `count` | `count` 或 `compare` | `kind`、可选 `pose/region`、比较时 `other_kind` | 根据注册表算答案 |
| `identity` | `identify` | `kind`、`answer_copy_id`、`anchor_blocker_object` | 根据复制源 mesh 及地标绑定算答案 |
| 其他八类 | `evidence` | `kind`、`declared_answer`、`evidence_ids` | 只保存草稿；标 `needs_review`，绝不当已验证 gold |

**难度与题型正交：** 三档均可选择 10 类中可行的一类；相同类型可在不同场景出不同难度。简单题的答案证据在起始视角不可见。中等题最多新增两个目标，至少一个必须被近处自然物体明显挡住。困难题先由 `anchor_query` 在至少两个候选地标中唯一选出最近的地标，再去该地标绕开局部遮挡寻找答案。困难档的第二个遮挡目标/干扰项可以增加辨别力，但不是硬性要求。程序用目标表面射线检测遮挡：至少 10 条有效射线，指定物件阻挡不少于 35%，遮挡物的水平跨度不超过场景 45%，而且它距目标不能过远。`anchor_query` 计算候选 mesh 中心的世界 XY 距离，最近与次近至少相差次近距离的 15%。

`evidence` 模式的 `declared_answer` 来自模型，**未经过客观答案器复核**。管线仍可导出 GLB、题目、起始视角和待审核答案，但状态必须是 `needs_review`；只有全部题目有可计算答案并通过几何/遮挡检查时才是 `candidate`。人工 gold 还需要审查自然性、颜色纹理语义、地标是否清楚和盲测轨迹。视角覆盖不等于「至少 30 次工具调用」。
