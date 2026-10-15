# 模型接口与替换模型

Pipeline 只需要一个能看多张图、按 Prompt 返回 JSON 的模型。各阶段的语义规则和 Prompt 不依赖某个模型名；模型适配层将它转换为统一的多模态 chat 请求。当前没有让模型直接执行任意 shell 或操作本机 Blender。

## Codex：订阅账号与自定义 provider

默认 `SCENEQA_MODEL_BACKEND=codex`。两种认证方式分别处理，不会自动降级或切换账号。

| 配置 | 行为 |
|---|---|
| `SCENEQA_CODEX_AUTH=chatgpt` | 校验本机 ChatGPT 登录；隔离 API key、base URL 等环境覆盖，使用官方 provider |
| `SCENEQA_CODEX_AUTH=configured` | 读取你私有 Codex 目录中的配置，保留 provider 的 API key 环境变量；不要求先用 ChatGPT 登录 |
| `SCENEQA_CODEX_HOME` | 指定已有 Codex 配置 / 登录目录；仅影响子进程，不改全局账号 |
| `SCENEQA_CODEX_PROVIDER` | configured 模式下可指定 provider ID；不设置则沿用 Codex 配置 |
| `SCENEQA_MODEL` | 模型名；兼容旧 `SCENEQA_CODEX_MODEL` / `LUNA_MODEL`，优先级依次降低 |
| `SCENEQA_CODEX_REASONING` | 默认 `xhigh`；自定义模型可用自己的档位，`inherit` 或空字符串不附加覆盖 |
| `SCENEQA_CODEX_TIMEOUT` | 单次 Codex 等待秒数；原题默认 240，followup 两个入口默认 480 |

单独阶段暴露了 `--model` 时，命令行值覆盖环境。没有此参数的阶段（例如 `qa` / `followup`）用 `SCENEQA_MODEL`。后台启动器支持 `--model`、`--backend`、`--reasoning`，不会强制恢复成某个 GPT 模型。

自定义 provider 按 [Codex 官方配置](https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers)设置，仓库示例：[codex-provider.example.toml](../examples/codex-provider.example.toml)。选择 Responses 协议，凭据通过 `env_key` 引用私有环境变量。完整配置放在自己的 Codex 目录，仓库不会复制它。

```bash
export SCENEQA_CODEX_AUTH=configured
export SCENEQA_CODEX_PROVIDER=sceneqa_custom
export SCENEQA_MODEL=your-vision-model
export SCENEQA_CODEX_REASONING=inherit
# 私有环境中另设 SCENEQA_PROVIDER_API_KEY。
sceneqa workflow config
sceneqa workflow auto --out runs/custom-model --target 4 --max-scenes 100
```

Codex 适配器使用 `codex exec` 的图片附件、结构化输出和无会话持久化模式，关闭 shell、联网搜索及其他 agent 工具。自定义 provider 需要支持 Codex 使用的 Responses 协议、多图和 JSON Schema 输出；仅有 chat/completions 或纯文本模型不能据此假定兼容。若需要协议转换，请在你自己的网关实现，不会暗中丢弃图片。

## 直接 HTTP API

如果你的模型暂不能经 Codex 调用，可显式选择：

```bash
export SCENEQA_MODEL_BACKEND=relay
export SCENEQA_MODEL=your-vision-model
export SCENEQA_API_URL=https://your-provider.example/v1/chat/completions
# 私有环境中另设 SCENEQA_API_KEY。
sceneqa workflow auto --out runs/http-model --target 4
```

这是完整的多模态 chat/completions URL。旧 `LUNA_*` 变量仍兼容，但没有私有默认地址，没有从 K8s Secret 找模型 key 的逻辑。该模式绕过 Codex；它与“模型经 Codex 接入”是两个可明确选择的后端。

## 扩展接口

`scene_factory/model_backend.py` 定义 `ModelBackend`：

```python
class ModelBackend(Protocol):
    model: str
    status: dict

    def complete(self, payload: dict) -> dict: ...
```

`payload` 的格式与 OpenAI-compatible 多模态 chat 一致：`model`、`messages`、可选 `response_format` 等。图片是按顺序带标签的 inline PNG/JPEG/WebP。返回至少包含 `choices[0].message.content`，其内容是完整 JSON 字符串；可附加真实 `usage`，不要伪造缓存命中量。

可安装自己的适配器模块，再配置 `SCENEQA_MODEL_BACKEND=plugin`、`SCENEQA_MODEL_FACTORY=your_package.adapter:create_backend`。工厂以关键字 `model=...` 调用，返回上面的接口。例如：

```python
class MyAdapter:
    def __init__(self, model):
        self.model = model
        # 此 ID 由你自己的非敏感 provider/version 配置生成；更换配置后应改变。
        self.status = {"auth_method": "my-provider", "version": "v1",
                       "account_fingerprint": "provider-account-config-v1"}

    def complete(self, payload):
        # 将有序文本/图片转给自己的客户端，再映射实际响应。
        result = my_client.generate(payload)
        return {"choices": [{"message": {"content": result.json_text}}],
                "usage": result.usage}


def create_backend(*, model):
    return MyAdapter(model)
```

这段是接口示意，`my_client` 需自行实现；仓库内置可工作的实现是 `CodexBackend`。插件必须自行实现凭据获取、超时和线程安全；身份检查可能再次构建适配器，所以工厂不应产生付费调用。`status` 仅含非敏感诊断与稳定指纹，不放 token、API key 或带凭据 URL。

内置 localhost gateway 只绑定 `127.0.0.1`，每次运行产生临时凭据，连接现有阶段客户端与 `complete(payload)`。它不公开服务，也不要求 K8s。默认 Codex 同时只执行一个请求，其余请求走重试；更换接口后不会自动增加并发。

## 缓存、审核与验证范围

缓存绑定图片、Prompt、模型及接入指纹，生产状态还绑定代码与渲染配置。换模型、provider、认证身份或 pipeline 版本时用新的输出目录；旧批次用原冻结代码续跑。configured 模式记录的是配置指纹，不读取第三方账号身份，因此不能承诺检测服务端在相同配置下的所有变更。

模型答案和审核默认使用相同模型的不同上下文。审核会看到候选答案，存在锚定偏差；这仍是模型意见，不是人工真值。本次整理验证接口路由、配置保留、图片顺序、重试、缓存和交付约束，不代表任意第三方模型的视觉能力已经验收。
