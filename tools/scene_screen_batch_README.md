# 模型场景留删筛选 v4

实现位于 `scene_factory/screening/`，完整规则见[筛选说明](../docs/SCREENING_RULES.md)。默认正文为 `prompts/v4.txt`，旧 `tools/prompts/scene_screen_zh.txt` 是兼容链接。

来源发现不再按面数、材质、纹理、标题词或元数据类别判断质量。模型看真实八视角，明显稀少可直接reject；拟保留时必须请求真实3D局部取证，再输出内容物、类别、区域及证据。试行20个完整物件、8类、3区域；数量是模型估计。

```bash
python3 tools/scene_screen_batch.py \
  --queue scene_uids.txt --sources sources.jsonl \
  --captures captures --output screening_results \
  --model gpt-6.1-sol --workers 3
```

密钥用 `LUNA_API_KEY` 或 `--key-file`；地址用 `LUNA_CHAT_URL` 或 `--url`。每个来源行含uid与绝对source_glb，便于模型要求局部取证。初始八图为四方位045/135/225/315的overview和closer，命名见规则。所有3D取景在K8s，本地不渲染。

输出 `v4/keep.jsonl`、`reject.jsonl`、`failed.jsonl`、`summary.json`、静态图册，以及逐件正文、原始响应、取证请求、相机、新图和结构化内容清单。来源名称不进入模型视觉判断。keep/reject是模型意见；failed是处理问题，不能冒充reject；删除只写清单，不移动原资产。

v4缓存按GLB、初始图、正文、门槛、取证预算、渲染器、模型和接口区分，补图完整性也参与检查。成功缓存可续跑，失败可重试；旧记录归档。429/5xx有三次请求预算。`local_cache_hit_ratio` 表示本地复用，`provider_cached_tokens` 只取服务端实际返回，历史与本次新usage分开。

仅有图片且请求真实取证时会failed；不能拿裁图替代3D操作。v1/v2/v3历史协议仍可显式选择。新来源/筛选协议使用新目录，旧生产使用自己的冻结快照。
