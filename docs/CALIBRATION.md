# 用人工标签校准监督者

`candidate` 和监督者 `pass` 都不是正确答案的证明。要决定监督循环能否用于批量交付，先让人工在同一批题上独立检查**题目是否可解**、**模型答案是否正确**，再比较监督者结论。抽样和预检流程见[监督循环说明](SUPERVISOR_LOOP.md)。

先从已完成的工作目录导出逐题 CSV：

```bash
sceneqa audit-template --runs-root /data/runs --output /data/human-labels.csv
```

每个 `output/validation.json` 的 simple、medium、hard 各占一行。人工填写：

| 列 | 允许值 | 含义 |
|---|---|---|
| `human_verdict` | `pass` / `review` / `fail` | `pass` 表示题干明确、初始视角不泄露、探索后有可见证据；`fail` 表示关键条件不成立；`review` 表示人工也无法确定。 |
| `human_answer_correct` | `yes` / `no` / `unknown` | 根据 GLB、题干和视角独立判断生成器答案；有明确错误填 `no`，证据不足填 `unknown`。 |
| `human_note` | 自由文字 | 说明遮挡、地标、答案或自然性问题，最好写具体对象和视角。 |

CSV 的 `asset_sha256` 与 `evidence_sha256` 绑定当时的 GLB 和四张截图；题目或答案改变也会让旧标签失效。命令默认拒绝覆盖已有 CSV；只有确实要重新创建空表时才用 `--force`。人工填表前应避免直接照抄生成器或监督者的结论。

评估命令：

```bash
sceneqa audit-evaluate --labels /data/human-labels.csv \
  --runs-root /data/runs --output /data/audit-metrics.json
```

其中 `false_accept` 是**人工判 `fail` 或明确答案错误，而监督者仍判 `pass`**的题数；`false_accept_rate_among_pass` 用它除以有明确人工结论的监督者通过题数。`false_accept_wilson95_upper` 是该比例的 Wilson 95% 上界，用来提醒小样本下零误收也并不意味着风险为零。`review` 是弃判，不能混入通过率；`legacy_supervisor` 是旧版看过生成器答案的报告，单独计数，不混入盲审结果。`stale`、`unlabeled`、`human_uncertain` 和 `missing_supervisor` 各有单独计数，不会被默认为正确或错误。

本地旧产物试跑已导出 2 个场景、6 道题的空白标签表 `scene-qa-runs/iterations-2026-09-25/human-label-template.csv`。评估正确返回 `unlabeled=6`、误收率为 `null`；这仅验证了工作目录到 CSV 再到统计的入口，**没有真实监督准确率证据**。当前环境没有 TokenHub key，暂不能执行 DeepSeek/GLM 的盲审试验。
