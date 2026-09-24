# 单场景三档增强计划格式

`plan.json` 必须是 UTF-8 JSON。世界坐标和对象名来自 `inspect/inventory.json`。三档从同一个原始 GLB 独立构建，不能在上一档上叠加。`entities` 必须穷举与题目有关的**原始**物件；`copies` 只引用这些物件。程序按注册表算答案，但原始物件是否清点完整仍须独立复核。新任务使用 `schema_version: "1.1"`；`1.0` 只用于读取早期样例。

```json
{
  "schema_version": "1.1",
  "scene_id": "example",
  "variants": [
    {
      "level": "simple",
      "entities": [
        {"id": "bottle_original", "kind": "bottle", "objects": ["BottleMesh"], "pose": "upright", "region": "shelf"},
        {"id": "cup_original", "kind": "cup", "objects": ["CupMesh"], "region": "shelf"}
      ],
      "copies": [
        {"id": "bottle_added_1", "from": "bottle_original", "xy": [0.3, 1.2], "support_object": "ShelfTop", "yaw_deg": 30, "pose": "upright", "region": "shelf"}
      ],
      "question": {"mode": "count", "kind": "bottle", "text": "架子上共有多少个直立的瓶子？", "pose": "upright", "region": "shelf"},
      "start_camera": {"azimuth_deg": 20, "elevation_deg": 30}
    },
    {"level": "medium", "entities": [], "copies": [], "question": {}, "start_camera": {},
     "occlusion_targets": [{"target_copy_id": "目标复制件 ID", "blocker_objects": ["原场景内用作遮挡物的 mesh 名称"]}]},
    {"level": "hard", "entities": [], "copies": [], "question": {}, "start_camera": {}}
  ]
}
```

上方仅说明字段，空的中、难档不是有效计划。中档用 `pose` 或 `region` 作为条件；难档的 `question` 设为 `{"mode":"compare","kind":"A","other_kind":"B","text":"A 和 B 哪种更多？"}`。比较答案为较多的 `kind`，数量相同则是 `equal`。三档的目标 `kind` 和题目文本都必须不同，最终答案也必须不同。复制件的 `xy` 是支撑面的水平坐标，程序向下射线求接触高度；不接受浮空手填 `z`。

**中档 1.1 的重点是视觉遮挡。** 只新增 1–2 个目标物件，至少一个放在场景原有的、语义自然的近处道具后面（例如蘑菇伞盖、花盆、长椅），而不是靠围墙把整个场景挡住。`occlusion_targets` 指定目标复制件和遮挡它的原始 mesh 名。构建器从起始相机向目标表面采样射线；至少 10 条有效射线中，指定遮挡物必须挡住不少于 35%，遮挡物的水平跨度不超过场景的 45%，并且其命中位置与目标足够近。同时，该目标不能在起始视角直接被粗粒度可见性探针发现，且换视角后必须可发现。这个射线门槛是视觉难度的代理指标，最终仍需看图、盲测。

简单 / 中等 / 困难起始视角分别至少遮住 1 / 1 / 3 个复制件，并确保全部复制件能由 24 个全局环绕视角，或由局部换焦、放大后的视角发现。

本协议先支持 **原场景物件复制、数量和比较题**。它有意不把 VLM 自报答案当真，也不把视角覆盖等同于「至少 30 次操作」。人工复核必须检查自然性、原始实例完整性、题意和实际解题轨迹。
