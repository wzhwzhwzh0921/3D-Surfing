# Codex 账号与自定义模型

模型接入的现行完整说明已整理到[模型接口](MODEL_INTERFACE.md)。默认使用 ChatGPT 登录；设置 `SCENEQA_CODEX_AUTH=configured` 后沿用你自己的 Codex provider 与凭据，不要求 ChatGPT 登录。模型名由 `SCENEQA_MODEL` 配置，后台启动器也支持 `--model`。

```bash
codex login status
sceneqa workflow config
```

默认渲染已改为本地子进程，K8s 是可选后端，见[渲染配置](RENDERING.md)。已有生产批次继续使用自己的冻结版本，不随新默认值改变。
