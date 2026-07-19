# SVG Logo Generation — LoRA Fine-tuning

Gemma 3 270M Instruct + LoRA → 详细提示词 → SVG 徽标生成。

## 文件

| 文件 | 内容 |
|------|------|
| `adapter/` | LoRA 权重（r=16, 3.8M params）|
| `reward.py` | SVG 质量评估函数（有效性/结构/相关性/退化检测）|
| `train_config.yaml` | 训练超参数配置 |
| `results.json` | 基座 vs 微调 验证集评估 |
| `report.md` | 实验报告与分析 |
| `svgs/` | 17 条验证集生成结果（基座/微调/GT 对照）|

## 结果

| 指标 | 基座 | 微调 | 提升 |
|------|------|------|------|
| mean reward | 0.048 | 0.475 | +0.43 |
| valid rate | 0% | 53% | +53% |

基座模型完全不会 SVG。微调后过半样本能画出合格徽标，高质量样本已接近 Sonnet 水平。
