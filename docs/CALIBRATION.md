# 用人工标签校准监督者

`candidate` 和监督者 `pass` 都不是正确答案的证明。要决定监督循环能否用于批量交付，先让人工在同一批题上独立检查**题目是否可解**、**模型答案是否正确**，再比较监督者结论。抽样和预检流程见[监督循环说明](SUPERVISOR_LOOP.md)。

先从已完成的工作目录导出逐题 CSV：

```bash
sceneqa audit-template --runs-root /data/runs --output /data/human-labels.csv
```

每个 `output/validation.json` 的 simple、medium、hard 各占一行。人工填写：

| 列 | 允许值 | 含义 |
|---|---|---|
| `human_start_leak` | `yes` / `no` / `unknown` | **只看起始图**能否答题；`yes` 是明显捷径，评价时计作坏题。 |
| `human_verdict` | `pass` / `review` / `fail` | `pass` 表示题干明确、初始视角不泄露、探索后有可见证据；`fail` 表示关键条件不成立；`review` 表示人工也无法确定。 |
| `human_answer` | 自由文字 | 审核员先独立看图作答，再查看生成器答案；看不清可留空并记录原因。 |
| `human_answer_correct` | `yes` / `no` / `unknown` | 根据 GLB、题干和视角独立判断生成器答案；有明确错误填 `no`，证据不足填 `unknown`。 |
| `human_note` | 自由文字 | 说明遮挡、地标、答案或自然性问题，最好写具体对象和视角。 |

CSV 的 `asset_sha256` 与 `evidence_sha256` 绑定当时的 GLB 和四张截图；题目或答案改变也会让旧标签失效。命令默认拒绝覆盖已有 CSV；只有确实要重新创建空表时才用 `--force`。人工填表前应避免直接照抄生成器或监督者的结论。

不想逐行编辑 CSV 时，可以生成本地逐题审核页：

```bash
sceneqa audit-review --labels /data/human-labels.csv \
  --runs-root /data/runs --output /data/runs/human-review.html
cd /data/runs && python3 -m http.server 8000
```

打开 `http://localhost:8000/human-review.html`，页面先只显示题目与起始图，记录能否直接回答后才展开可旋转 GLB 和补充视角；先填写自己的答案再揭示生成器答案。点击“导出标注 CSV”后，把下载的文件作为 `audit-evaluate --labels` 输入。页面只在当前浏览器本地保存草稿，不会改动原 CSV；**必须导出下载文件**才算保存正式标注。若题目、答案、GLB 或证据图已被返修改动，页面会拒绝旧 CSV，需重新导出模板。页面通过 CDN 加载 `<model-viewer>`，离线时仍可看静态图。网页交互是降低标注负担的工具，不是独立的人类 gold；标签需要实际审核员填写。

评估命令：

```bash
sceneqa audit-evaluate --labels /data/human-labels.csv \
  --runs-root /data/runs --output /data/audit-metrics.json
```

其中 `false_accept` 是**人工判 `fail`、起始图泄露答案或明确答案错误，而监督者仍判 `pass`**的题数；`false_accept_rate_among_pass` 用它除以有明确人工结论的监督者通过题数。`start_leak_yes` 单独报告起始图答案捷径的数量；若同一行又把题目质量标为 `pass`，会计入 `human_inconsistent` 并退出准确率分母，待人工修正。`false_accept_wilson95_upper` 是该比例的 Wilson 95% 上界，用来提醒小样本下零误收也并不意味着风险为零。`review` 是弃判，不能混入通过率；`legacy_supervisor` 是旧版看过生成器答案的报告，单独计数，不混入盲审结果。`stale`、`unlabeled`、`human_uncertain` 和 `missing_supervisor` 各有单独计数，不会被默认为正确或错误。

本地旧产物试跑已导出 2 个场景、6 道题的空白标签表 `scene-qa-runs/iterations-2026-09-25/human-label-template.csv`。评估正确返回 `unlabeled=6`、误收率为 `null`；这仅验证了工作目录到 CSV 再到统计的入口，**没有真实监督准确率证据**。当前环境没有 TokenHub key，暂不能执行 DeepSeek/GLM 的盲审试验。

页面还用本机 Chromium/Playwright 做过交互冒烟：6 张题卡加载，未填写起始图判断时拒绝展开；填写后可打开 3D、输入独立答案、揭示生成器答案，刷新后本地草稿仍在。导出的带 UTF-8 BOM CSV 已重新送入 `audit-evaluate`，程序正确读到 1 条示例标注、5 条未标注，并因缺少监督报告而保持准确率为 `null`。这条示例标注只用于检查交互和文件格式，**不作为人工 gold**。
