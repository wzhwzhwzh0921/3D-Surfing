# 单场景候选题与机位探测

正式构建前，agent 在**同一次会话**中写 `candidates.json`，每档两个候选，合计六个。格式为：

完整、可运行的结构示例见 [`candidate_fixture_pool.json`](../examples/candidate_fixture_pool.json)；其中的物件和答案只是自制小场景样例，不能照搬到其他 GLB。

```json
{
  "schema_version": "candidate_pool_1",
  "candidates": [
    {
      "candidate_id": "simple_a",
      "level": "simple",
      "start_landmarks": [
        {"label": "一楼圆形餐桌", "objects": ["TableTop", "TableCloth"]}
      ],
      "entities": [],
      "copies": [],
      "question": {}
    }
  ]
}
```

上面的空 `entities/copies/question` 只是字段位置示意；候选须填完整的 v1.3 `variant` 字段，参照 [计划协议](PLAN_SCHEMA.md)。`start_camera` 在候选阶段可留空。`start_landmarks` 写题干要求探索者**开始探索时首先找到**的原始 mesh；困难题只需把第一步参照物列为起始地标，其他候选地标可以靠后续旋转、换焦来发现。把尚需探索的所有物体强求在起始图同框，会误杀有效难题。候选 ID 只能用字母、数字、下划线和连字符。

```bash
sceneqa probe --source /data/source.glb --candidates /data/candidates.json --output /data/probe
```

`probe_report.json` 为每个候选扫描全局方位与仰角，记录起始地标是否可见、投影尺寸、可见表面采样比例、隐藏的复制件、局部遮挡射线的第一命中物、困难题最近地标关系，并给出两张推荐机位预览 `候选ID/start_1.png`、`start_2.png`。地标须至少有 35 像素投影范围和 25% 可见表面采样比例，才通过预设的起始地标门槛；不通过时，`failure_reasons` 标出失败条件，并尽量同时给出地标较清楚和目标被遮住的两张预览，便于修改候选。`eligible` **不保证画面语义清楚或答案正确**。agent 必须打开候选图确认题干地标确实可认，尤其不能把仅露出一角的桌子当作可辨地标；两道都不行时修改候选再探测。

最后从每档选一题，写 `plan.json`：保持 `schema_version: "1.3"`，加入 `workflow_version: "candidate_probe_v1"`，每个 variant 保留 `candidate_id`，填入探测通过的 `start_camera`。选中候选的摆放、题干、地标等字段须与探测时相同；若改变，先重新运行 probe。最终三题仍须题型、答案各不相同，答案由模型在正式构建后看图填写。

`select` 现在要求报告使用 `landmark_surface_v2` 机位协议；旧探测报告需重新运行 `probe`，以免沿用只露一角却被判为合格的起始视角。
当起始地标本身不清楚，或目标已经从起始角度露出时，探测器会跳过这一角度的局部遮挡射线细算，并在 `failure_reasons` 中标为 `local_occlusion_not_checked`；只有完成细算且确实不满足要求时才标为 `local_occlusion_missing`。

可用 `sceneqa select --candidates /data/candidates.json --probe-report /data/probe/probe_report.json --simple simple_a --medium medium_b --hard hard_a --output /data/plan.json` 自动复制候选，避免手工重抄字段。每档默认采用 `start_1.png` 对应机位；若第二张更好，给相应档位加 `--simple-view 2` 等参数。只允许选择 `eligible` 的预览。

正式构建额外生成每档 `answer_view.png`。模型应把计数所需的所有原件和复制件写进 `question.evidence_ids`；区域边界、方向参照物、最近地标等原始 mesh 写进 `question.evidence_context_objects`。构建器据此扫描局部机位，尽量把证据同框，并在 `build_report.json` 的 `answer_view_coverage` 记录可见组数。覆盖不全时应改题或改摆放；程序不会据此计算答案。
