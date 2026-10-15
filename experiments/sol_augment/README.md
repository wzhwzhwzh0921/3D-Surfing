场景增难实验：输入一个 GLB 或每行一个 GLB 路径或 JSONL `{uid, source_glb}` 的队列。程序通过可配置渲染后端处理3D（默认本地子进程，K8s可选）：给模型八张原景图与最多 16 个原生物件组联系图；模型用短 JSON（带target类别）规划三份不同增强；按配置后端复制原物件并保存 GLB、渲染成品；一次联合调用让模型看三档实际 start/reveal/region（共9张）回答、自查。默认最多两轮，即失败后修订一次；`SCENEQA_AUGMENT_ROUNDS=3` 则允许修订两次。

```bash
export LUNA_API_KEY=...
python3 scene-qa-factory/experiments/sol_augment/run.py \
  --source /absolute/path/scene.glb --out scene-qa-runs/augment
# 只生成远程证据，不调用模型
python3 scene-qa-factory/experiments/sol_augment/run.py \
  --source /absolute/path/scene.glb --out scene-qa-runs/evidence --inspect-only
# 批量：每行一个路径；20个场景是测试分母，允许 unsuitable
python3 scene-qa-factory/experiments/sol_augment/run.py \
  --queue scenes.txt --out scene-qa-runs/augment20
```

统一入口推荐 `sceneqa workflow augment`，继承模型与渲染配置，见 [README](../../README.md)。直接调用本脚本时可使用 `LUNA_MODEL`、`LUNA_CHAT_URL`、`LUNA_API_KEY` 或 `LUNA_KEY_FILE`；缺少凭据直接报错，不读取 K8s Secret。渲染默认本地，显式设置 `SCENEQA_RENDER_BACKEND=k8s` 才需要自己的 namespace、镜像和 kubectl。

每档交付位于最终 `round_N/built/simple|medium|hard/`：`scene.glb`、`question.json`（问题、模型答案、起始相机、起始/揭示图片文件名），对应 PNG。根目录 `result.json` 指向最终轮次。各调用保留原始 API response、usage 和输入哈希缓存；远程证据缓存校验 source SHA256、worker SHA256 与八图齐全；增强缓存再校验 plan SHA256。重复同一模型输入复用缓存，渲染已落盘则复用；已有终态 result 会跳过，修改 prompt 做新实验请使用新 out 目录。自查通过仅表示模型视觉自评，不是独立 gold 验收。

原物件按节点名的前两段合组，只提供小尺寸组；联系图用于让模型确认是否完整。没有适合的组、节点过多或无法自然出三档会明确记 unsuitable。新计划只需 `ids + support + at:[u,v]`，其中 u/v 是所选支撑 XZ 包围盒内的 0..1 位置，程序计算位移。支撑对齐使用所选原生 mesh 的中心竖直射线局部命中，未命中则要求修复布局；NumPy 实现不依赖 rtree，它不是全物件碰撞求解，实际自然摆放与遮挡仍由模型看成品判断。答案不会由代码计数或计算。

完成后专用 pod 可释放：`kubectl -n "$SCENEQA_NAMESPACE" delete pod scene-sol-augment-20260930`。

汇总完整测试分母、每件各轮、三GLB产物、所有原始调用用量（包括格式错误的成功HTTP响应）：

```bash
python3 scene-qa-factory/experiments/sol_augment/report.py \
  --out scene-qa-runs/augment20 --queue scenes.jsonl
```

JSONL 的 uid 优先作为目录名，要求唯一且不含路径字符。更改模型、URL、提示词或 worker 后，已有终态不会直接复用，新产物放 version_hash 子目录保留旧记录；做对照实验推荐使用新 out。

JSONL 可带 `feedback` 一句已有独立图审失败事实，供对照修复；此字段也进入调用/终态缓存指纹。默认每轮只有一次规划调用、一次三档联合视觉回答调用，不让自查改已指定的起始/揭示相机。

支撑 bbox 内也可能是板缝或空区：真实 triangle 未命中会报出坐标，不把 bbox 最高点当落地。`unsuitable` 反映当前提供的原生候选组，不能证明整个资产不存在可用物件。报告中的上游 cached token 与本地输入缓存分别记录。

同 out 版次选择由 `UID/current_run.json` 指定：`{"run_dir":"."或"version_<hash>","status":"running|complete|error|unsuitable"}`。报告/导出须读取该相对目录；running/error 不可回退旧版成功。历史目录保留，main终态 stdout 的 run_dir 返回实际目录。

仅修语言歧义，不重建场景：
```bash
python3 scene-qa-factory/experiments/sol_augment/revise_question.py \
  --result /absolute/path/result.json --critic /absolute/path/record.json \
  --out /absolute/path/new_revision/UID
```
只修改独立审核 bad_levels 的 question/answer，复用原GLB/相机/图像，其他题保持原样。新题不继承旧自评通过，须独立审核。

中等题闭环接口：`run_scene(remote, source, out, initial_feedback="", focus="medium", rounds=1)` 仅生成、构建和回答medium一道；首个有效成品不论自评是否通过均返回给外部审核，rounds只用于技术规划/构建重试；旧focus=all默认不变。`repair_medium.repair(result_path, critic_path, out, remote=None, source=None)` 返回新result路径；critic judgment 的 repair_action 是 question/view/scene。每次一次规划、一次看成品回答，修题或换已有视图无需K8s，scene只通过配置后端构建medium，输入可仅medium或原三档，其他存在档原样保留。新目录保存独立revision，不覆盖旧产物。
