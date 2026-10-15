# 第二轮模型抽查

先选 stdout 已终态且三档 GLB 均存在的场景，队列一行一个 UID。不要审仍在 repairing 的场景。

```bash
python3 tools/independent_qa_audit.py \
  --baseline /绝对路径/sol-augment20-v3 \
  --queue audit_uids.txt --output /绝对路径/independent-audit \
  --url https://your-provider.example/v1/chat/completions \
  --model gpt-6.1-sol --key-file /安全位置/relay-key --details
```

可用 `LUNA_API_KEY` 代替 key-file。只发送 UID、三档 question/answer 和实际 start/reveal 图片，`--details` 加入已有局部图；不发送生成模型 pass、理由或 Luna 意见。

三档起始均不得直接作答；简单至少一次视角变化，中等真实局部遮挡，困难唯一线索后定位不同隐藏目标。检查三题内容不同、答案可见支持、摆放自然；证据不足不能算通过。

输出 `{uid,pass,bad_levels,reason}`，保留 prompt、原始响应、usage 与输入图片路径。缓存按图像、题目、prompt、模型及URL区分；最多三次重试。summary区分本轮新usage与记录历史usage。这里只是另一次模型调用的审阅意见，不是人工gold，不计算答案、不修改 acceptance 或生成脚本。

v4有限对照使用 `--workers 4 --regions --max-attempts 1`：每套至多一个独立模型响应，不做质量重抽。处理错误、缺图/缺GLB记录failed，源场景明确unsuitable单列；二者都不当视觉质量reject。输出自动包含progress.json与中文gallery.html。

v5补齐原要求：三题对象及问法都不同；困难题删除第一跳后若剩余地址仍唯一定位则fail，实际图中至少两个相似候选需第一跳选一个。此prompt用于新产物；旧模型PASS原文保留并标覆盖局限，不通过重抽挑选判断。

最后question-only复测可加 `--overviews`，提供同套实际四方位总览来检查指代与候选区域。prompt要求审完全部三题，即使simple失败也检查中难题；理由分别说明三档。没有单独报告的条件不推断为证实。新revision使用fresh audit输出，旧意见不覆盖。

## 当前版本优先

若UID目录有 `current_run.json`，审核只读取其受限相对 `run_dir` 下的 result、终态和来源metadata。拒绝绝对路径、含 `..` 的路径及symlink越界。pointer为running/error时不调用模型、不回退UID根旧成功；unsuitable单列，complete但缺当前result/终态也不回退。相对GLB路径按当前run目录解析，各level图片按当前result明确引用的GLB目录读取；绝对GLB引用可用于question-only共享不可变artifact，不从旧result推断。当前目录参与缓存哈希，记录run_dir/current_run/result_path/source_metadata。无指针旧基线保持兼容。

已用真实临时目录与mock HTTP验证：旧成功+新complete读取新题/图片/metadata；running/error/unsuitable、父路径/绝对路径/symlink越界均无模型调用。未访问真实API或重抽历史。

中等题可独立审核：`--level medium` 只发送该题的问题、候选答案及实际 start/reveal（可加 `--regions`）图片。只检查唯一指代、起始真实局部遮挡、合理视角改变后答案证据与自然摆放；物件小不算遮挡。简单/困难及题型重复不影响中等题判断，不要求两跳。默认 `--level all` 保持三档审核。模式进入缓存指纹及摘要，图册标明“中等题单题审核”，通过不代表整套通过。

```bash
python3 tools/independent_qa_audit.py --baseline /path/to/run --queue /path/to/uids.txt --output /path/to/medium-audit --level medium --regions --workers 4 --max-attempts 3
```

每题版本仅一次有效质量判断；负面结论不会重抽，成功响应格式异常也不重新调用。临时网络错误可重试（最多3次，尊重 Retry-After）。API key 使用环境变量 `LUNA_API_KEY` 或仓库外 `--key-file`。审核复用远程生成的PNG，不渲染、不修改题目答案或人工验收。

中等题返修协议：`--level medium` 输出额外字段 `repair_action`。模型通过必须 `pass=true,bad_levels=[],repair_action="none"`；不通过必须 `pass=false,bad_levels=["medium"]`，由模型选择 `question`（证据足够，只修指代/措辞/候选答案）、`view`（从已有视图改选隐藏起始/清晰揭示）或 `scene`（真实遮挡、摆放、完整物件需改3D）。理由包含具体返修要求，程序不按关键词推断类别。格式或一致性错误记录程序 `failed`，不进行质量重抽；controller只使用 `status="ok"` 的判断。新增prompt进入缓存指纹，旧medium协议不会复用；`all`仍输出原四字段。此改动不修改已经完成的15题历史意见或统计。
