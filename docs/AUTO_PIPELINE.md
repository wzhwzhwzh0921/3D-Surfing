# 自动原题生产子流程（workflow auto）

需要从网上自动找素材、最终得到多目标 / 两跳定位复合任务时，使用[完整批量主线](FOLLOWUP_BATCH.md)：

```bash
.venv/bin/python tools/start_followup_batch.py --out runs/online-200 --target 200
```

该主入口会自行补充素材及原题，不要求种子原题。**本文的 `workflow auto` 是只生产原题的子流程，不必在完整批量主线前手动执行。** 想单独研究原题时再使用下面的命令；其 `--target` 统计原题交付数，和完整主线的复合任务计数不同。

一个入口按顺序完成：在线发现素材 → 下载和文件预检 → 配置后端多视角渲染 → 模型留删筛选 → 原景出题（按配置允许原生物件增强） → 模型看图回答 → 独立审核 → 反馈返修及复审 → 交付。当前每个场景生产一道中等题；困难题不是保留门槛，也未宣称当前自动入口稳定交付三档题组。

```bash
codex login status
python3 -m scene_factory workflow auto \
  --out /data/sceneqa-auto --target 100 --batch-size 4 --max-scenes 2000
```

依赖 Python 3.10+ 和配置的模型后端；本地默认安装 `.[batch,render]`。K8s 是可选后端，需要时才安装 kubectl 并提供自己的镜像、命名空间。见[模型接口](MODEL_INTERFACE.md)、[渲染配置](RENDERING.md)和 `.env.example`。

## 在线搜集

默认 `prosceneverse-search` 每个新任务从 [Stable-X/ProSceneverse](https://huggingface.co/datasets/Stable-X/ProSceneverse) 重新下载约14 MB的公开 `metadata.parquet`，分别检索市场摊位、码头作业、营地庭院、室内工作间和居家场景。根据 UID 与本轮下载的 Objaverse `object-paths.json.gz` 连接，得到实际 GLB 地址；数据集的 `file_name` 是内部归档路径，不能用作 Objaverse 路径。实际场景逐件从网上下载，不读取旧素材池。元数据需要 `.[batch]` 中的 pyarrow；当前入口覆盖这一公开来源，不能将其称作已经遍搜所有网站。

程序保留原资产的来源页面、作者、单件许可证、类别和文件元数据。来源阶段仅检查实际GLB地址、原有在线许可/来源要求、已知体积与去重，不再按面数、材质、纹理、标题关键词或类别判断场景质量。类别仅用于均衡取样，缺失则归入 UNKNOWN。所有内容是否丰富的判断交给模型看真实场景图。

每次新任务使用新的 `--out`，在线响应在该任务的 `discovery_cache/` 保存快照，只用于断点恢复。`--exclude-history old.jsonl` 仅读取历史 UID 来排除重复，不读取其中的 GLB 或筛选结果。同一检索页按名称/描述提示安排下载顺序，排序不是验收；所有候选仍由模型看真实3D图决定留删。网络错误不能解释成来源耗尽。新增关键词可用 `--query tavern --query garden`，多个词为“或”；变更发现配置或判断协议时使用新的 `--out`。

仅需复现历史素材时才显式使用 `--catalog`（本轮重新收集不使用）：

```bash
python3 -m scene_factory workflow auto \
  --catalog catalog/qualified.jsonl --out /data/existing --target 100
```

JSONL 支持 `uid`／`scene_id` 与 `url`／`glb_url`／`download_url` 别名。作者和来源等额外字段保持原样。稀疏历史条目可使用，但不会凭缺失元数据推断质量。不同 UID 的相同 GLB 内容按 SHA-256 去重。

## 留删、出题与返修

每批默认四条来源。下载和文件预检后生成真实多视角图片，模型按 v5 探索信息协议筛选；拟保留时必须主动请求局部3D换焦、旋转或缩放。模型需提供至少三道不同目标、至少两种题型、有实际视图证据的问题，涉及至少两个内容不同的区域。物件数量/类别只作记录，不强制20件/8类。大片破面、稀疏外壳及重复装饰仍删除。只有保留场景进入正式出题；同一场景的当轮图片通过哈希校验后可以用于后续出题，不复用历史场景。

模型优先使用原场景已有内容设计一道起始角度无法直接回答、具有实际局部遮挡的中等题；计划 `copies=[]` 保留原景，确需调整时才复制完整原生物件自然增强。没有独立复制组的扫描场景仍可以出原景题。程序构建 GLB，模型查看真实图片填写答案。第二个独立上下文审题、答案证据、遮挡与摆放，默认仍使用同一模型，并非不同模型族交叉验证；失败时明确返回 `question`、`view` 或 `scene` 返修动作，再由生成模型修改和看图作答，随后重新审核。默认最多返修两次，仍失败就剔除。详细协议见 [Sol 两步流程](SOL_TWO_STAGE.md)。

程序不计算或核验语义答案。API／下载／渲染／文件错误写入 `failed.jsonl`，不能当成题目质量剔除；已知超大和内容重复可在下载预检阶段剔除。相同题图及协议的第一次有效审核意见被缓存，FAIL 和 PASS 都复用，不能反复询问原样题目直到获得 PASS。

`--target` 指**最终通过独立模型审核且文件完整的题目目标**。默认新在线检索任务还需达到室外 `ceil(target/2)`、室内 `floor(target/2)` 的交付配额；标签来自实际图像，检索词仅为提示。`--min-outdoor / --min-indoor` 可调整。只够总数、尚未达到环境配额时继续搜索；通过审核的所有结果在 `keep.jsonl`，最终按配额选出的目标题组在 `selected.jsonl`。下载成功或初筛通过不计作交付。

## 断点、预算与失败重试

选择日志将批次来源与推进后的发现游标一起原子落盘。重启先继续已选批次，不重新随机取素材；生成与模型请求使用各自指纹缓存。已经交付的结果会检查文件完整性和哈希，不凭旧状态计数。独占输出锁防止两个进程同时写同一批次。

同一协议内中断后使用同一命令续跑。新来源协议为 `sceneqa-discovery-v2-model-screen`，不能复用旧预筛游标或将旧来源清单解释成新规则选取的来源；升级使用新输出目录，从原始来源重新准备。旧 `--max-faces` 仅为命令兼容接受，不生效。历史处理失败可以显式重试：

```bash
python3 -m scene_factory workflow auto \
  --out /data/sceneqa-auto --target 100 --batch-size 4 --max-scenes 2000 \
  --retry-failed
```

该参数重试处理失败，不重新抽样旧的质量剔除。429／5xx 在网络调用层有有限重试。连续多个批次全为处理错误时停止，避免接口或集群故障导致无休止地下载和调用。默认只使用调度器专属的 screen／qa worker，退出时释放这两个 worker，不处理集群其他任务。默认渲染在本地子进程执行，也可显式配置 K8s。

| 参数 | 默认值 | 含义 |
| --- | ---: | --- |
| `--target` | 100 | 最终模型审核通过的交付数量 |
| `--batch-size` | 4 | 每批发现并筛选的来源数 |
| `--max-scenes` | 5000 | 本任务最多下载处理的不同来源条目 |
| `--max-scan` | 10000 | 发现游标累计扫描的元数据行数 |
| `--max-pages` | 100 | 发现游标累计读取页数 |
| `--max-bytes` | 64 MiB | 单 GLB 下载体积预算，不用于判断内容复杂度 |
| `--max-repairs` | 2 | 每题质量返修次数上限 |
| `--max-failed-batches` | 3 | 连续整批处理错误后停止 |
| `--stage-timeout` | 1800 秒 | 一个筛选批次或一件出题阶段的运行时限 |

到达下载数量、元数据扫描或分页预算而目标未完成时会明确报告预算耗尽；来源真实结束单独报告 `source_exhausted`。这些状态不是完成目标，也不会用初筛条目凑交付数。`--max-scenes` 与目标可以在续跑时提高；变更查询、模型、来源、判断协议或发现窗口预算时用新输出目录。

## 输出和可视化

```text
output/
  campaign.json / progress.json / summary.json
  registry.jsonl / keep.jsonl / selected.jsonl / reject.jsonl / failed.jsonl
  gallery.html
  discovery_cache/ / review_cache/
  batches/batch_00001/
    batch.json / sources.jsonl
    screen/                  下载、初始图、模型取证与逐件留删意见
    qa/                      生成、审核、返修历史及最终交付
```

最终保留行的 `folder` 指向 `scene.glb`、`question.json`、`start.png`、`reveal.png`、`audit.json`。题目 JSON 包含模型答案和初始相机。根图册直接在图片下显示题目和答案，并链接原 GLB、每批筛选与审核记录。`pending` 仅表示流水线尚未出题，不是需要人工决策的候选池。

模型的 PASS 是该协议下的视觉审核意见，不等同于程序证明答案正确。已知漏检、原始响应、返修理由和历史版本保持可追溯。

## 2026-10-01 真实在线测试（旧预筛协议）

运行统一入口，设置目标 1、批量 2、处理上限 4，未手动指定某个已知好场景。程序从在线元数据读取 100 条记录，选择并下载四个场景。第一批的空建筑与散件被模型剔除，调度器自动补充第二批。第二批两个场景通过场景筛选，街边餐车场景进入出题及两次返修，最终交付一道中等题后停止。另一个场景保留为待出题状态，提高目标后才继续。

| 项目 | 实测 |
| --- | --- |
| 初次全链路耗时 | 508.44 秒，约 8 分 28 秒 |
| 处理素材 | 4 个，分两批 |
| 最终状态 | 1 个交付、2 个剔除、0 个处理失败、1 个待出题 |
| 示例题 | 探索3D后，推车靠近木桌一侧的下层隔间内、起始视角被侧板遮挡的物体是什么？ |
| 模型答案 | 灰色盆 |
| 审核 | 独立上下文给出 PASS，保留前两轮未通过记录 |
| 实际调用模型 | `gpt-6.1-sol` |
| 交付文件 | GLB、题目及相机、起始图、揭示图、审核 JSON 均完整 |
| 同一命令重跑 | 约 0.085 秒；下载、原始模型响应与远程渲染记录未改动 |
| 集群任务 | 只释放本次调度器的两个 worker，成功 |

工作区测试产物在 `/home/ubuntu/blender/scene-qa-runs/sol-auto-online-smoke-20261001/`。`live_verification.json` 保存首次运行统计和文件完整性检查，`resume_verification.json` 保存重跑检查，`gallery.html` 直接显示题目与两张实际视角图。文件头和哈希检查只检验交付完整性，不推断语义答案。

这一次验证证明了在线发现、自动补充、出题、返修、最终交付及终态续跑的连接；样本规模不足以估算长期接受率或千题生产成本。

## 回归检查

网络 mock、来源恢复、筛选故障和控制器恢复检查不调用模型、不加载或渲染几何，也不核验语义答案：

```bash
python3 -m unittest discover -s tests -p 'test_online_sources.py' -v
python3 -m unittest discover -s tests -p 'test_scene_pipeline_intake.py' -v
python3 -m unittest discover -s tests -p 'test_scene_automation.py' -v
python3 -m unittest discover -s tests -p 'test_scene_feedback_loop.py' -v
```
