# 单场景三档增强计划格式

`plan.json` 必须是 UTF-8 JSON。世界坐标和对象名来自 `inspect/inventory.json`。三档从同一个原始 GLB 独立构建，不能在上一档上叠加。`entities` 必须穷举与题目有关的**原始**物件；`copies` 只引用这些物件。程序按注册表算答案，但原始物件是否清点完整仍须独立复核。

```json
{
  "schema_version": "1.0",
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
    {"level": "medium", "entities": [], "copies": [], "question": {}, "start_camera": {}},
    {"level": "hard", "entities": [], "copies": [], "question": {}, "start_camera": {}}
  ]
}
```

上方仅说明字段，空的中、难档不是有效计划。中档用 `pose` 或 `region` 作为条件；难档的 `question` 设为 `{"mode":"compare","kind":"A","other_kind":"B","text":"A 和 B 哪种更多？"}`。比较答案为较多的 `kind`，数量相同则是 `equal`。三档的目标 `kind` 和题目文本都必须不同，最终答案也必须不同。复制件的 `xy` 是支撑面的水平坐标，程序向下射线求接触高度；不接受浮空手填 `z`。每档起始视角要至少遮住 1/2/3 个复制件，并确保 24 个环绕探测视角合起来能发现全部复制件。

本协议先支持 **原场景物件复制、数量和比较题**。它有意不把 VLM 自报答案当真，也不把视角覆盖等同于「至少 30 次操作」。人工复核必须检查自然性、原始实例完整性、题意和实际解题轨迹。
