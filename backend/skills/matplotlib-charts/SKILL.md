---
name: matplotlib-charts
description: 使用 Matplotlib 根据用户数据生成柱状图、饼图和折线图，并输出 PNG 图片。适用于数据对比、占比展示、趋势分析、把 JSON 数据可视化，以及用户明确要求画图、制作统计图或生成图表图片的场景。
---

# Matplotlib 图表

使用 `scripts/render_chart.py` 生成图表，不要重复编写临时绘图脚本。

## 工作流

1. 确认图表类型：
   - 分类数值对比：`bar`
   - 构成或占比：`pie`
   - 时间序列或趋势：`line`
2. 将数据整理为 UTF-8 JSON 配置文件。
3. 运行：

```powershell
python backend/skills/matplotlib-charts/scripts/render_chart.py `
  --input chart-data.json `
  --output output/charts/chart.png
```

从 `backend` 目录运行时，将脚本路径改为
`skills/matplotlib-charts/scripts/render_chart.py`。

4. 检查命令返回的输出路径、文件大小，并在可用时查看生成图片。
5. 最终回复提供下载链接：

```markdown
[下载图表](http://localhost:3000/api/local/downloads/output/charts/chart.png)
```

## JSON 格式

柱状图和折线图使用 `labels` 与 `series`：

```json
{
  "type": "bar",
  "title": "季度销售额",
  "x_label": "季度",
  "y_label": "万元",
  "labels": ["第一季度", "第二季度", "第三季度"],
  "series": [
    {"name": "华东", "values": [120, 150, 180]},
    {"name": "华南", "values": [100, 140, 160]}
  ],
  "show_values": true
}
```

将 `type` 改为 `line` 即可生成折线图。折线图可设置
`"show_values": true` 显示数据标签。

饼图使用 `labels` 与 `values`：

```json
{
  "type": "pie",
  "title": "市场份额",
  "labels": ["产品 A", "产品 B", "其他"],
  "values": [45, 35, 20],
  "show_values": true
}
```

通用可选字段：

- `width`、`height`：图片尺寸，默认 `10`、`6`
- `dpi`：分辨率，默认 `160`
- `colors`：Matplotlib 颜色列表
- `legend`：是否显示图例，默认 `true`
- `grid`：柱状图、折线图是否显示网格，默认 `true`
- `show_values`：是否显示数值或饼图百分比

## 约束

- 只接受有限数值，拒绝 `NaN` 和无穷值。
- 所有系列长度必须与 `labels` 一致。
- 饼图数值不得为负，且总和必须大于零。
- 标签较多时扩大图片宽度，必要时简化标签，避免文字重叠。
- 使用描述性文件名，默认输出到 `output/charts/`。
- 不调用 `plt.show()`；脚本使用无界面的 `Agg` 后端。

## 依赖

若缺少 matplotlib，在后端虚拟环境安装：

```powershell
python -m pip install matplotlib
```

依赖无法安装时，明确告知用户，不要声称图表已生成。
