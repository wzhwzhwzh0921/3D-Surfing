# 候选探测流程冒烟测试（2026-09-25）

验证对象包括仓库自制的 `examples/fixtures/hidden.glb`，以及上一轮视觉拒收的 Low Poly House Interior 源资产。后者只在本地工作区留图和 GLB，不提交原始资产。

- 自制 GLB：`probe` 处理六道候选，输出 12 张起始图和约 13 KB 的摘要报告，**没有导出 GLB**；简单题存在可用机位，中、困难题因指定局部遮挡物不是实际第一遮挡物而无可用机位。`select` 正确拒绝这两档。
- Low Poly House：基于六道候选重新探测后，简单题有 1 个可用机位，中等题的两道候选各有 2 个，困难题候选 A 有 4 个。`select` 复制简单 A、中等 B、困难 A，生成三档计划；随后真实 Blender 构建与 `verify` 得到结构性 `candidate`。三档 `answer_view.png` 均包含计划声明的证据组，其中简单题的四把椅子比旧版同框图更易清点。
- 视觉边界：简单题推荐起始图里的餐桌仍只露出一部分，中等题的白色坐垫仍可能与沙发自身的坐垫混淆。因此这个实验**只证明新工具链能完成探测、选择、构建与结构校验**；没有证明三题已达到视觉入库标准，也没有执行 DeepSeek/Claude Code 单会话端到端调用。

本地截图与报告：`../scene-qa-runs/workflow-v2-demo/index.html`。验证命令：

```bash
python3 -m unittest discover -s tests -q
python3 -m scene_factory.cli probe --source examples/fixtures/hidden.glb \
  --candidates examples/candidate_fixture_pool.json --output /tmp/sceneqa-probe
```
