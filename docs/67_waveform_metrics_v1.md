# 波形指标（v1）

**状态**：设计说明。本文件说明 `pe_sim.waveform_metrics` 从**一个已求解窗口的样本**
里能测出什么、怎么测、以及哪些数字它拒绝给。

## 1. 为什么是"测量"而不是"模型"

本项目的分工是：使用者与 agent 搭建电路、器件参数与控制算法；我方提供基座与参考
设计。在这个分工下，**给每个拓扑写一套损耗公式，等于把拓扑重新写回内核** —— 而这
正是 `docs/43` 不变量 I5 要避免的。

能推广的只有一件事：**测量**。给定一份编译电路的样本和它来自的描述符，回答每个工程
师都会问的量 —— 输入功率、负载功率、两者之间的损耗、效率、实测占空比、电感电流
统计、逐信号摘要。同一段代码对 buck、反相 buck-boost、以及这个项目从没见过的电路
都成立，因为它不知道拓扑名。

## 2. 能量平衡

一个窗口内的瞬时能量关系是

$$\int v_\text{in} i_\text{in}\,dt \;=\; \int \frac{v_\text{out}^2}{R}\,dt \;+\; \Delta E_\text{stored} \;+\; E_\text{dissipated}$$

因此模块报告：

| 量 | 来源 |
| --- | --- |
| `input_w` | 每个独立直流源的 $\frac{1}{N}\sum (-v\cdot i)$ |
| `output_w` | 每个 `resistor_load` 的 $\frac{1}{N}\sum v^2/R$ |
| `storage_rate_w` | $(\Delta E_\text{stored})/\text{span}$，$E=\sum \frac12 C v^2 + \sum \frac12 L i^2$ |
| `loss_w` | `input − output − storage_rate` |
| `efficiency` | `output / input`，**仅在窗口是工作点时** |

**储能项不能省。** 从一个未充电的电容起步的窗口里，`input − output` 会把充进电容的
能量算成损耗。实测（D = 0.5，Vin = 12 V，L = 100 µH，C = 470 µF，R = 10 Ω，
fsw = 100 kHz，20 ms 窗口）：

```text
input=4.65857 W  output=4.28584 W  storage=0.426902 W  loss=-0.0541685 W
efficiency=unmeasured（储能变化占输入功率 9.2%）
```

不加储能项时这个窗口会报出 0.37 W 的"损耗"，其中 0.43 W 其实是电容储能。

## 3. 拒绝给数而不是给假数

- **窗口不是工作点时不给效率。** 判据是 $|\Delta E/\text{span}| / P_\text{in} \le 5\%$。
  启动窗口的"效率"没有意义，把它算出来只是把一个瞬态包装成一个指标。被抑制时
  `efficiency` 是 `null`，`reason` 写明储能占比并建议加长窗口或使用周期窗口。
- **缺信号不等于零。** 每个量在缺数据时给出 `null` + `reason`（例如"源电流不在这张
  表里"）。报 `0 W` 会与"真实测到零"无法区分，而这正是本层要避免的失败模式。
  聚合量（如 `input_w`）只说"不完整"，明细在各分量的 `reason` 里。
- **负损耗照实报告。** `loss_w < 0` 表示采样模型"耗散了负能量"，那是数值伪影
  （开关瞬间的梯形积分振铃），不是物理。它不被截断到零，而是在
  `quality.loss_is_physical` 里标为 `false`。
- **没有唯一参考网时整体不出数。** `ok: false` 并说明原因，因为端子电压没有定义。

## 4. 符号约定是标定过的

ngspice 的 `i(V...)` 以"流入源正端"为正，所以**交付功率是 `−v·i`**。这不是按注释
猜的：`tests/test_waveform_metrics.py` 在一个真实求解的 buck 上核对 ——
`i(v1)` 均值 $-0.388$ A、$v_\text{in}=12$ V，得 `input_w` = 4.659 W，与负载功率加
储能变化一致。把符号写反会让 `input_w` 变成负数，测试会立刻失败。

## 5. 实测占空比

对每个 `pulse_source`，`observed_duty` 是**控制网电压高于载波幅值一半的样本占比**。
它是从波形里数出来的，不是从描述符里读的，因此可以用来核对"命令的占空比"与"电路实际
看到的占空比"是否一致 —— 这也是 `--carrier-phase` 那类参数是否被正确接上的第一道
证据。实测 0.49956（命令 0.5）。

## 6. 主频与电感电流

- `switching_frequency_hz` / `switching_period_s` 取描述符里载波的声明值（不是从波形
  推断的），因为载波就是这个频率。
- `inductors[].current_a` 给 `min`/`max`/`mean`/`rms`/`peak_to_peak`/`last`。
  **注意**：`peak_to_peak` 包含启动浪涌与开关瞬间的数值尖峰，不等于稳态纹波。

## 7. 物理验证

不是"公式跑通了"，而是"测量对物理变化有响应"：把开关导通电阻从 1 mΩ 提到 0.5 Ω，
同一份描述符、同一窗口，测得的总损耗单调上升：

```text
Ron=1m    input=4.66847  output=3.58796  storage=1.13054  loss=-0.0500289
Ron=0.05  input=4.56358  output=3.58481  storage=1.01356  loss=-0.0347889
Ron=0.2   input=4.26241  output=3.57565  storage=0.682459  loss=0.00430525
Ron=0.5   input=3.73992  output=3.55925  storage=0.123608  loss=0.0570617
```

（200 µs 窗口，$i_L(0)=0.62$ A，$v_C(0)=5.95$ V，D = 0.5。理想开关下 −0.05 W 的残余
来自开关瞬间的积分振铃，约 1% 输入功率。）

`tests/test_waveform_metrics.py` 钉住两件事：合成数据上的**恒等式**（
`input = output + storage + loss` 精确成立），与真实求解上的**单调性**
（Ron 增大 → 损耗增大 ≥ 0.05 W）。

## 8. 接线方式

- `circuit_compile` 现在为**每个独立源**保存电流（`i(V1)`、`i(V2)`…），否则输入功率
  根本无法测量；`i(V2)` 是栅极驱动电流，实测为 0（`SW` 模型输入阻抗无穷大），
  被单独列出而不混进效率。
- `pe-sim simulate-topology` 的报告新增 `metrics` 字段。
- 库层直接调用：`waveform_metrics(descriptor, table, element_names=dict(compiled.element_names))`。

## 9. 边界与非主张

- **一个窗口，不是稳态。** 所有数字都是这个窗口的积分；窗口没到周期性，测到的就是
  瞬态。这一点写在 `assumptions` 里，也写在 `quality.window_is_operating_point` 里。
- **损耗是总量，不是分摊。** 不做开关/二极管/磁件的分项归属：那需要每个器件的电流，
  而把电流通道按拓扑拼出来，等于把拓扑写回内核。
- **不替代解析模型。** `pe_sim.lossy_converter` 的解析损耗模型仍在，用于它能覆盖的
  场合；本模块回答的是"任意电路跑完之后，这次运行到底消耗了多少"。
- **不升级证据等级。** 这些数字是 `limited`：它们能证明"这次运行的能量守恒"，不能
  证明模型被评审过。

## 10. 相关文档

| 文档 | 关系 |
| --- | --- |
| `docs/64_circuit_topology_authoring_v1.md` | 描述符、器件词表、网表编译 |
| `docs/65_circuit_execution_v1.md` | `simulate-topology` 与报告字段 |
| `docs/21_stage10_restricted_metrics_export_v1.md` | 受限指标导出（不同层） |
| `docs/22_stage12_evidence_levels_v1.md` | 证据阶梯与 `limited` 的含义 |
| `docs/releases/v0.24.0.md` | 本次发布说明 |
