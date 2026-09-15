# auto-assign · 通用任务派单（人 / AI 混排）

把一批任务，按「类型 × 规模 → 工作量」规则表、执行者的可用性与能力组、工作日历和现有占用，
排成均衡排期；**先出预览，确认后才写回**飞书多维表格，或导出 CSV 人工导入。

**不限于设计团队。** 执行者可以是人也可以是 AI agent：人在工作日与节假日约束下排，
AI 按 7×24 排、日容量单独配，两者混在同一张成员表里、用同一套规则表竞争同一批任务。

## 为什么还要一个

公开的飞书 skill 只做 CRUD/发消息；公开的任务调度 skill 面向 **AI agent 集群**调度。
没有一套是"给人排期 + 工时规则表 + 节假日日历 + 容量倒排 + 人机混编 + 需求模板自适应"的组合。

## 30 秒上手

```bash
cp config/*.example.* config/            # 复制示例配置
python scripts/init_template.py --config-dir config --emit-fields
python scripts/plan.py --config-dir config --outdir out     # 预览
python scripts/plan.py --config-dir config --outdir out --adapter feishu --apply
```

飞书凭证走环境变量（不落盘）：

```bash
export FEISHU_APP_ID=cli_xxx
export FEISHU_APP_SECRET=xxx
export FEISHU_APP_TOKEN=base_xxx
export FEISHU_TABLE_TASK=tblxxx
export FEISHU_TABLE_MEMBER=tblxxx
```

## 两条核心设计

**1. 不设固定负载阈值，只说业务目标**

```yaml
launch_date: 2026-09-15
finish_before_launch_days: 3
```

阈值越小 → 摊得越开 → 每人峰值越低、收尾越早（单调）。
所以引擎二分搜索"满足 deadline 的**最小**负载上限" = 能按时交付前提下的最均衡解。
压不进去就报警，不硬排。

**2. 项目标识用分类字段，不用标题前缀**

大写字母前缀（D01/C12）把"哪个 app"和"是否完成"两个语义塞进名字，难筛难统计。
改成「项目分类」单选 + 「是否完成」标签两个独立字段。

**3. 人和 AI 用两套成本模型**

人按天（`日成本` × 工时），AI 按次（`单次成本`，与耗时无关）——
API 计费本来就不是按小时算的，套人天模型会得出错误结论。
订阅制 AI（如包月套餐）在成员表填 `订阅费用 / 订阅周期 / 预估次数`，引擎自动摊成单次 = `订阅费 ÷ 预估次数`。

预览里直接给「这批 AI 任务若全改由人做要多少钱」的差额，这是"该不该交给 AI"的判据。

AI 也可以具名：`ai_naming: model_task` → 任务表里显示 `gpt-5·首页主banner`，
同时保留 `执行者`（真实调度槽）与 `模型` 两列，展示名再花哨也不混淆调度单位。

## 脱敏说明

本仓库的示例数据（成员、需求名、类型）全部为占位/通用词汇，
不含任何真实业务名称、真实人名、真实 app_token 或凭证。
真实配置请放在 `config/` 下且**不要提交**（见 `.gitignore`）。

## 目录

```
config/     配置与示例（field_map / members / workhours / requirements / schedule / aliases）
engine/     planner.py 排期引擎（确定性算法）+ fetch_holidays.py
adapters/   feishu_adapter.py / csv_adapter.py
scripts/    plan.py 编排入口 / init_template.py 模板生成器
references/ feishu-api.md 接入细节与错误码
```

## 许可

MIT
