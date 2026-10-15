# 渲染后端：本地默认，K8s 可选

主线使用同一份 `experiments/sol_augment/worker.py` 处理 GLB，生成标准八视角、相机、清单，以及需要时的原生物件复制。局部探索使用同一 worker 与 `screen_view_worker.py` 的组合，支持 bbox 换焦、旋转和缩放。

后端只决定这个 worker 在哪里执行，不决定如何筛选或出题。`scene_factory/rendering.py` 提供 `RenderWorker.create/run/close`；`screen`、`qa`、`followup`、`auto` 和两个生产调度器共享这个接口。兼容入口里的 `Remote(...)` 现在只是工厂名称，并不意味着必定远程。

## 本地运行

```bash
pip install -e '.[render,batch]'
export SCENEQA_RENDER_BACKEND=local
export SCENEQA_RENDER_THREADS=2
export PYOPENGL_PLATFORM=egl
sceneqa workflow config
sceneqa workflow screen --sources sources.jsonl --out runs/local-screen
```

不调用 kubectl，不读取 Kubernetes Secret，不需要集群。3D 引擎只在 worker 子进程里导入，主调度进程不加载它。每次渲染等待子进程完成，超时会终止自己的进程组；局部取景串行化。

| 变量 | 默认 | 用途 |
|---|---|---|
| `SCENEQA_RENDER_BACKEND` | `local` | `local` / `k8s`，非法值直接报错 |
| `SCENEQA_RENDER_PYTHON` | 当前 Python | 可使用另一份安装渲染依赖的虚拟环境 |
| `SCENEQA_RENDER_THREADS` | `2` | 本地数值库 / 软件渲染线程限制，不是严格 GPU 或内存配额 |
| `SCENEQA_RENDER_TIMEOUT` | `1800` | 单次 worker 超时秒数 |
| `PYOPENGL_PLATFORM` | `egl` | 本地 EGL 或 `osmesa`；需要对应系统驱动 |
| `EGL_DEVICE_ID` | pyrender 默认设备 | 多个 EGL 设备时选择设备编号 |
| `LIBGL_ALWAYS_SOFTWARE` | 未设置 | 可设 `1` 强制 Mesa 软件渲染，必须选中软件 EGL 设备 |

Linux headless EGL 需要能创建 OpenGL 上下文的驱动。Ubuntu 起点：

```bash
sudo apt-get install libegl1 libgl1 libgl1-mesa-dri libglib2.0-0
```

也可安装 `libosmesa6`，选择 `PYOPENGL_PLATFORM=osmesa`；具体是否可用取决于系统 Mesa / PyOpenGL 版本。Python 依赖无法替代系统图形驱动。错误时查看产物下 `local.log`；不会自行安装驱动，也不会静默改走另一台机器。

没有 GPU 时可使用 Mesa llvmpipe。若机器同时暴露硬件和软件 EGL 设备，先查询设备编号：

```bash
PYOPENGL_PLATFORM=egl python -c 'from pyrender.platforms.egl import query_devices; print(list(enumerate(query_devices())))'
```

选择软件设备的 `EGL_DEVICE_ID` 后设置 `LIBGL_ALWAYS_SOFTWARE=1`。必要时用 `__EGL_VENDOR_LIBRARY_FILENAMES` 指定本机 Mesa vendor JSON，例如 Debian 的 `/usr/share/glvnd/egl_vendor.d/50_mesa.json`。编号及路径按本机环境确认，不要固定套用测试机器的编号。错误 `Not allowed to force software rendering when API explicitly selects a hardware device` 表示仍选中了硬件设备。上述本地 OpenGL 选择也计入缓存指纹。

当前主线是 `trimesh + pyrender`，不是 Blender Cycles。Blender 专有节点材质、体积或模拟不能假定与 GLB 表现一致；原始材质会尽量保留，复杂 shader 的渲染限制应在抽查时确认。早期 `sceneqa run/batch` 使用 Blender，可用 `SCENEQA_BLENDER` 指定可执行文件（默认查找 `blender`）；它们不是推荐的新生产入口。

线程限制无法避免大模型耗尽内存。使用 `--max-bytes` 控制资产大小并从少量场景开始。批量调度中的 `--parallel` 是队列并发，不受每个 worker 的线程数自动约束。

## K8s 运行

无需部署 pipeline 服务，只需要本机 kubectl 能访问自己的集群，并具有 Pod 的创建、查询、等待、exec、cp、删除权限。`kubectl cp` 需要镜像包含 tar。模型调用和下载仍在主调度机，模型 key 不传入渲染 Pod。

镜像只需包含 Python、NumPy、trimesh、pyrender、Pillow、SciPy 和适当的 EGL / OpenGL 驱动。构建示例：

```bash
docker build -f deploy/render/Dockerfile -t your-registry/sceneqa-render:tag .
docker push your-registry/sceneqa-render:tag
export SCENEQA_RENDER_BACKEND=k8s
export SCENEQA_NAMESPACE=your-namespace
export SCENEQA_RENDER_IMAGE=your-registry/sceneqa-render:tag
sceneqa workflow auto --out runs/k8s-originals --target 10
```

| 变量 | 默认 | 说明 |
|---|---|---|
| `SCENEQA_NAMESPACE` | `default` | 你的命名空间 |
| `SCENEQA_RENDER_IMAGE` | 无 | K8s 模式必须明确提供 |
| `SCENEQA_IMAGE_PULL_SECRET` | 无 | 可选，不设则不写 imagePullSecrets |
| `SCENEQA_RENDER_GPUS` | `1` | GPU 数；设 0 去掉 GPU 请求，需软件渲染驱动 |
| `SCENEQA_RENDER_CPU_REQUEST` / `CPU_LIMIT` | `2` / `4` | CPU 资源 |
| `SCENEQA_RENDER_MEMORY_REQUEST` / `MEMORY_LIMIT` | `4Gi` / `10Gi` | 内存资源 |

上表的缩写 `CPU_LIMIT` 等完整变量名均带 `SCENEQA_RENDER_` 前缀。提供的通用 Debian 镜像是依赖起点，不承诺所有 GPU 节点都兼容；NVIDIA GPU 还需要集群 runtime / device plugin 和宿主图形驱动支持，可按你的节点改成合适基础镜像。

worker 上传后校验 SHA256，场景文件按需上传；结果取回本地，worker 的临时目录随后清理。生产器只清理自身命名的 Pod，借用的已有 Pod 不由局部取景器删除。集群不可用记为处理失败，不回退本机。选择 K8s 时，worker 在导入 3D 引擎前检查集群环境及 service account。

## 切换与旧批次

新仓库默认本地，但既有生产批次保留其冻结代码及 K8s 配置。整理仓库不会改动运行中的批次、登录、kubectl context 或 `.env`。在本机不适合渲染时，显式选择 K8s；这与仓库面向其他使用者的默认值是两回事。

新任务的生产 / 缓存指纹记录渲染后端和配置，换后端用新的输出目录，避免把不同光栅化环境生成的截图当作同一证据复用。测试使用 mock 后端和不含 3D 导入的轻量进程 fixture；这次整理没有在本机启动实际 3D 渲染。

另外提供 `sceneqa workflow smoke --out runs/render-smoke` 进行真实渲染自检，不需要模型账号或在线素材。2026-10-15 已在独立容器的全新虚拟环境中验证默认 local 后端及 Mesa llvmpipe；执行位置为 K8s，未在用户本机渲染，详见[可移植性验证记录](PORTABILITY_CHECK.md)。
