# 本地独立运行验证 · 2026-10-15

这里的“本地独立运行”指：完整克隆仓库后安装依赖，使用自己的模型账号 / API 和本机图形驱动即可运行；不依赖开发机的其他代码目录、既有场景库或我们的 K8s。它不意味着已经附带模型权重、在线素材和系统驱动，当前支持源码 editable 安装。

## 验证方式

1. 仅复制 Git 将发布的源码、Prompt、测试和仓库内 fixture 到另一个目录，不复制 `.env`、开发机虚拟环境、登录文件或任何历史产物。
2. 创建全新的 Python 3.11 虚拟环境，安装 `pip install -e '.[batch,render]'`。独立安装与 238 项回归测试通过。
3. 将同一份源码送入专用临时容器，在容器里再次创建独立虚拟环境并安装依赖。容器外层由 K8s 提供，实际运行时去掉 `KUBERNETES_SERVICE_HOST` 等标记，使用仓库默认的 **local** 渲染后端。
4. 使用 `sceneqa workflow smoke` 完成真实渲染。普通 EGL 和显式选择的 Mesa 软件 EGL 均通过；这不是 mock 渲染。软件测试确认 OpenGL renderer 为 `llvmpipe (LLVM 15.0.6, 256 bits)`，OpenGL 为 Mesa 22.3.6 / 4.5 Core Profile。
5. 在独立源码环境通过实际 Codex CLI 调用一次 `gpt-6.1-sol`，传入真实 fixture 渲染图，成功返回描述方形底板、墙板与三个小立方体的中文 JSON。未复制或改变账号登录。

所有真实 3D 渲染发生在 K8s 临时容器中，用户本机没有执行真实 3D 渲染。容器内 local 后端不使用 kubectl；外层 K8s 只是本次验证的执行环境。

## 实测结果

| 检查 | 结果 |
|---|---|
| 独立源码目录安装 | 通过 |
| 回归测试 | 238 项通过，不调用真实模型 / 渲染 |
| 八视角标准渲染 | 8 张 768×768 PNG，通过 |
| 原景交付渲染 | 8 张 PNG，通过 |
| bbox 换焦 + 旋转 + 缩放 | 1 张 PNG + 相机记录，通过 |
| 原 GLB 保留 | 输出与输入 SHA256 完全相同 |
| Mesa 软件渲染 | llvmpipe，通过，不要求 GPU |
| 实际 Codex 图像调用 | JSON 返回成功，约 11 秒 |

fixture 的 SHA256：`35bf289e7ab9aacb6fd14b0222c8e930d13ff0b8989b31450b1933792bb759d0`。渲染自检不生成正式题目、不调用模型，也不下载素材。完整问答闭环的回归测试使用模型 fixture；本次实际模型调用验证图像传递和结构化输出，**没有重新测量完整出题的质量或通过率**。

独立安装实际解析到的主要版本：NumPy 2.4.6、trimesh 4.12.2、pyrender 0.1.45、PyOpenGL 3.1.0、Pillow 12.3.0、SciPy 1.17.1、pyarrow 26.0.0。其他操作系统、驱动、依赖版本与第三方 provider 仍需在自己的机器执行自检，不能由这份记录推断全部兼容。

## 复现

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[batch,render]'
.venv/bin/sceneqa workflow config
.venv/bin/sceneqa workflow smoke --out runs/render-smoke
```

系统需要可用的 EGL / OpenGL 驱动，配置见[渲染说明](RENDERING.md)。通过后，在 `.env` 配置自己的 Codex provider 或 HTTP API，再按[README](../README.md)运行筛选及出题流程。
