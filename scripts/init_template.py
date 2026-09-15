"""根据 field_map + workhours 生成：需求模板 CSV、飞书表字段清单、成员/工时模板。

用法：
    python scripts/init_template.py --config-dir config --out config
    python scripts/init_template.py --emit-fields     # 额外输出建表字段清单 md
"""
import argparse
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from plan import load_yaml, read_csv, write_csv  # noqa: E402

FIELD_TYPE = {
    "name": "文本", "type": "文本", "requirement": "文本", "value": "文本",
    "output": "文本", "qty": "数字", "requester": "文本",
    "designer": "双向关联(→成员表)", "start": "日期", "end": "日期",
    "status": "单选", "project": "双向关联(→项目表)", "parent": "双向关联(→任务表自身)",
    "group": "单选", "hours": "文本", "priority": "单选",
    "category": "单选", "done": "单选/复选",
    "kind": "单选(human/ai)", "daily_capacity": "数字", "model": "文本",
    "concurrency": "数字", "cost_day": "数字(元/天)", "cost_run": "数字(元/次)",
    "cost": "数字(元/次)",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config-dir", default=os.path.join(ROOT, "config"))
    ap.add_argument("--out", default=os.path.join(ROOT, "config"))
    ap.add_argument("--emit-fields", action="store_true")
    args = ap.parse_args()

    fmap = load_yaml(os.path.join(args.config_dir, "field_map.yaml")) if os.path.exists(
        os.path.join(args.config_dir, "field_map.yaml")) else load_yaml(
        os.path.join(args.config_dir, "field_map.example.yaml"))

    # 1) 需求模板：表头来自 field_map，类型候选项来自工时规则表
    wh_path = os.path.join(args.config_dir, "workhours.csv")
    wh = read_csv(wh_path) if os.path.exists(wh_path) else read_csv(
        os.path.join(args.config_dir, "workhours.example.csv"))
    whmap = fmap["workhours"]
    types = sorted({r[whmap["type"]] for r in wh if r.get(whmap["type"])})

    reqmap = fmap["requirements"]
    header = [reqmap[k] for k in ("name", "type", "requirement", "value", "output", "qty", "requester")]
    sample = [{reqmap["name"]: f"示例{t}", reqmap["type"]: t,
               reqmap["requirement"]: "动态+静态", reqmap["value"]: "/",
               reqmap["output"]: "待定", reqmap["qty"]: "", reqmap["requester"]: ""}
              for t in types[:3]]
    write_csv(os.path.join(args.out, "requirements_template.csv"), sample)
    print(f"已生成 {args.out}/requirements_template.csv（{len(types)} 种类型：{', '.join(types)}）")

    # 2) 飞书表字段清单
    if args.emit_fields:
        lines = ["# 飞书多维表格字段清单（按此建表即可）", ""]
        for tbl in ("tasks", "members", "projects"):
            lines.append(f"## {tbl}")
            lines.append("| 字段名 | 类型 |")
            lines.append("|---|---|")
            for k, v in fmap[tbl].items():
                if k.endswith("_value"):
                    continue
                lines.append(f"| {v} | {FIELD_TYPE.get(k, '文本')} |")
            lines.append("")
        lines.append("""## 建表注意事项
- 状态单选建议取值：未开始 / 进行中 / 已完成 / 已取消（「已完成」取值写进 field_map.tasks.done_value）
- 成员表「状态」的在职取值写进 field_map.members.active_value
- 成员表加「类型(human/ai)」「模型」「并发数」「日成本」「单次成本」即可人机混排；
  并发数 > 1 时引擎会展开成 N 个调度槽（初稿Agent#1..#N），槽位只在内部使用，成员表里不用建这些行
- 任务表若要落成本，加「成本」数字列；写回时 preview 已按 规则表成本 > 单次成本 > 日成本 折算好
- 双向关联写入用字符串数组 `["recX"]`，对象形式 `{"record_id":"..."}` 会报 1254074
- 任务表数据量大时必须服务端 filter，全量拉取会超时且 total 不可信
""")
        p = os.path.join(args.out, "FIELDS.md")
        open(p, "w", encoding="utf-8").write("\n".join(lines))
        print(f"已生成 {p}")


if __name__ == "__main__":
    main()
