# 2026-09-24 试跑记录

所有凭据通过进程环境传给本地 loopback 协议桥；仓库和报告不保存真实 key。模型为 TokenHub 上的 `deepseek/deepseek-flash`，Claude Code CLI 输出以流式 Anthropic Messages 兼容格式接收。下列 cache 比例来自 TokenHub 的 `prompt_cache_hit_tokens`，不是估算。

| 案例 | 输入 | 结果 | Claude 会话 | API 调用 | 输入 / 命中缓存 token | 说明 |
|---|---|---|---:|---:|---:|---|
| 自制遮挡示例 | `examples/fixtures/hidden.glb` | `candidate` | 1 次，约 179 秒 | 20 | 220,834 / 199,936（90.5%） | 三份 GLB 和答案 `3、2、box`；程序独立重建、验收通过 |
| Anglo-Saxon House Diorama | 目录 `e651a307552648c8a40e87ab6c3c3712` | 900 秒超时 | 1 次 | 35 | 1,022,325 / 955,392（93.5%） | 32 个网格里仅木桶、旗帜两种明显可复制小物件；因此新增确定性预筛，后续对此资产直接标 `unsuitable` |
| Gnome/borrowers garden 初次低档试验 | 目录 `a450b90cb444431d93c6e4bab70fa233` | 600 秒超时；已有三档草案 | 1 次 | 29 | 579,369 / 521,472（90.0%） | 两档通过，难档一处复制件重叠、另一个局部物件不在 24 个全局视角里；后续构建器自动小幅移位并加入局部换焦探测，草案经独立重建成为 `candidate` |
| Gnome/borrowers garden 最终全新试验 | 同一目录 URL，重新从零启动 | `candidate` | 1 次，约 342 秒 | 17 | 291,545 / 250,752（86.0%） | 模型在同一会话里两次构建/验收并修正计划；退出后程序独立重建、最终三档全部通过。题目是硬币数量、平放纸夹数量、顶针与火柴棍比较；三份 GLB 大小约 5.96 / 6.09 / 6.02 MB |

这些实验还暴露语义风险：Gnome **初次草案**把原网格名含 `wormie` 的物件称为“企鹅”。程序会列出这种命名不一致，但不能据此自动确认物体类别；需要看图审核。最终全新试验的三档没有这类命名警告，但仍需人工检查原物件计数和摆放自然性。`candidate` 不意味着 gold answer 或 30 次操作难度已验证。

模拟上游服务测试覆盖了 Claude Code CLI → 本地协议桥 → OpenAI Chat Completions → 流式 Anthropic 响应，含图片、工具调用、65,536 输出 token 上限、缓存统计和 429 后重试。真实试跑期间没有发生 429，因此真实 429 的等待时长尚未实测。

## 1.2 协议的构建验收

`examples/ten_by_three_garden_plan.json` 是手工编写的 10 类题型协议样例，沿用 garden 场景原始 GLB。它没有经过 DeepSeek 自动规划；用途是验证新版结构、三次独立导出和判定逻辑。实际 Blender 构建与 `sceneqa verify` 均完成：简单题 `count` 答案 5，困难题 `identity` 答案为 `teacup_and_saucer`，程序均可从注册表与地标关系核对；中等题 `color` 的“蓝色”由样例计划声明，程序无法核实颜色语义。三档可见性检查均通过，困难题的最近地标距离分别约为 6.94 和 13.23（世界 XY 单位）。因此总状态是 `needs_review`，不是正式 gold。新版 10 类随机选择是否能由 DeepSeek 稳定完成，仍需单独实测。
