# 独立监督与定点返修（试验版）

默认批处理仍是一次生成会话。加 `--supervisor-rounds 1` 后，每个结构合格场景增加一次**独立、只读图像**的监督者审查；如果它指出可修问题，启动一次定点返修会话，重新执行候选探测、构建、结构校验，再由监督者复审。最多允许两轮返修。监督者只收到题目、题型和每题四张 PNG，不先看到生成器答案；它看不到生成计划、自评、库存或代码。程序核查监督者确实读取全部 12 张图，且没有读其他文件。生成器改动后仍必须通过原来的探测、图像读取和构建验证。

```bash
sceneqa run --input-path /data/source.glb \
  --output-root /data/runs --provider tokenhub --key-file /secure/tokenhub.env \
  --model deepseek/deepseek-flash --supervisor-model glm-5.3-flash \
  --supervisor-rounds 1
```

`--supervisor-model` 可选；不指定时使用生成模型，但仍是**独立上下文**，不等于跨模型验证。模型 ID 必须由同一个 provider/密钥支持。`--supervisor-rounds 0` 是默认值，暂不增加 API 成本；先用小样本校准后再扩批。

已有完整产物可先只做监督试验，不重跑生成器：

```bash
sceneqa supervise --work /data/runs/scene-id --repairs 0 \
  --provider tokenhub --key-file /secure/tokenhub.env \
  --model deepseek/deepseek-flash --supervisor-model glm-5.3-flash
```

确认监督报告有用后可改为 `--repairs 1`。返修要求当前候选报告使用新版协议；旧报告仍可 `--repairs 0` 只读审核，需重新探测后才能自动返修。对已有场景使用 `run --force` 会重新调用生成器；`supervise` 不会这样做。

监督者逐题输出独立答案、`pass / review / revise`、具体画面理由、证据视角；独立答案与生成器答案不一致或无法作答时，原本的 `pass` 降为 `review`，由人工裁定，不会自动改写生成器答案。程序只做保守的文本规范化后比较，语义相同但措辞不同的答案也可能进入人工复核。`revise` 还必须给出可执行的返修指令。`review` 表示图像仍有不确定性，进入 `supervisor_needs_review`，不会被算作通过。`revise` 会把**仅有问题的档位**交给返修会话；原始 GLB 和监督者已经判为 `pass` 的计划档位受程序保护。返修后如果结构不合法、改动了受保护内容，或复审没有改善，程序恢复返修前的资产并标记 `supervisor_rejected`。复审全部通过，且生成器自己的视觉自评也通过，批处理状态才是 `candidate`。

每轮保留 `supervisor_round_N.json`、只读工具日志和独立用量日志；返修阶段保留 `repair_round_N.jsonl` 与用量日志。状态文件还记录返修是否更换了题目、是否只修正答案，便于把“同题改善”和“换题后通过”分开统计。`report` 页面只显示与**当前 GLB 和 12 张证据图哈希**均相匹配的监督结论，避免回滚或重渲染后把旧问题误贴到当前资产上。备份仅在返修时临时存在，完成后清理。

这不是正式 gold 判定：监督者只看有限渲染图，虽然看不到生成器拟定答案，仍可能受题干暗示和固定渲染视角影响；同一个模型族也可能重复生成器的错误。应先对人工审核过的小样本报告监督者逐题准确率、误收率、`review` 比例、返修后通过率、API 用量与缓存命中率，再决定是否作为默认流程。3D-DefectBench 的[论文](https://arxiv.org/abs/2607.10826)将相机和提示格式作为评估变量，并用细粒度缺陷而非单一总分；[Cross-Model VLM-Judge](https://arxiv.org/abs/2606.18451)则说明独立模型族与呈现偏差控制的价值。本实现借鉴其测量思路，**未复现它们的多视角数量或人类对齐实验**。

审核样本先按**题**记录缺陷，但统计精度按**独立场景**保守计算：同一场景的三题共享源资产和生成过程，不能直接当成三个独立样本。首轮 20 个场景用于找明显问题，最多产生 60 道题；简单/复杂来源各 10 个，尽量覆盖室内、室外与题型。人工逐题记录 `pass / review / fail`，同时核对标准答案；再对同一批题跑监督者，统计误收（人工 `fail` 而监督 `pass`）、弃判和三档差异。只要发现明显误收，先改提示或证据视角，再在**未参与修改的新场景**上复测。模型判 `pass` 且有明确人工 gold 的场景才进入精度分母：在零误收的理想情况下，Wilson 95% 上界低于 10% 也至少需要 **35 个这样的独立场景**，因此首轮 20 个场景绝不能单独证明稳定。发生误收或审核弃判较多时，还要继续补样。正式批量使用前建议扩大到至少 100 个有可用产物的场景，并继续报告逐题、逐档和场景簇的风险；对监督者判 `pass` 的题抽样复核，对 `review/revise` 全量检查。以上是试验配额和风险目标，**不是已经测得的准确率**。每轮还需单列返修前后同一道题的净变化，不能把换题后的通过率当作返修成功。

可用以下命令生成可复现的首轮名单；仓库保存了一份种子 `260926` 的[20 场景试验名单](../catalog/audit-pilot-2026-09-26.jsonl)。每档 4 个明确室内、4 个明确室外、2 个分类待判，单一来源分类最多 2 个，同一作者只取 1 个，并排除超过 100 MB 的资产。`audit_environment=unclassified` 是诚实的未判状态，不应根据名称强行归类。名单选择不证明场景能成功增强；需要继续做下载、Blender 预览和人工筛选。

```bash
sceneqa audit-sample --catalog catalog/qualified.jsonl \
  --output /data/audit-20.jsonl --per-tier 10 --seed 260926
sceneqa audit-sample --catalog catalog/qualified.jsonl \
  --output /data/audit-next-20.jsonl --per-tier 10 --seed 260927 \
  --exclude-catalog /data/audit-20.jsonl
sceneqa batch --catalog /data/audit-20.jsonl --limit 20 --workers 3 \
  --output-root /data/audit-preflight --dry-run
sceneqa audit-gallery --catalog /data/audit-20.jsonl \
  --runs-root /data/audit-preflight --output /data/audit-preflight/index.html
```

最后一个命令把每个原场景的前、后、侧、俯视图排在一页，方便先删掉视觉过稀、主题不明确或物体身份难辨的样本；它展示的是**原始场景**，不是监督者审查三道增强题的页面。

`--exclude-catalog` 可重复给多个旧波次；新波次会排除旧场景及其作者，保持每波的来源配额。仓库保存了一个已排除首轮的[第二轮 20 场景名单](../catalog/audit-pilot-wave2-2026-09-26.jsonl)，尚未验证下载或可增强性。`audit-evaluate` 的 `scene_cluster_audit` 给出已获得人工 gold 且被监督者通过的独立场景数、含误收的场景数、Wilson 上界，以及**假设今后不再出现误收**时还需多少个有效场景。这个“追加数”是乐观下限，不是保证拿到这么多新源场景就能停；不适合增强、未完成审核或监督者弃判的场景都不进入分母。

[本地验证记录](SUPERVISOR_SMOKE.md)区分了已经通过的程序测试和仍需真实 API/人工样本验证的部分。
