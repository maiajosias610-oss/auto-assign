#!/usr/bin/env python3
"""开工前校验：确认派单输入是用户真实提供的，不是模板、不是猜的。

背景：plan.py 原先在 config 里找不到 members.csv / workhours.csv / requirements.csv 时，
会静默回落到 *.example.* 示例数据，跑出一个"看起来正常"但完全虚假的排期。
本脚本在跑排期之前把这道口堵上。

用法:
    python scripts/preflight.py --config-dir config
    python scripts/preflight.py --config-dir config --scaffold   # 顺手拷出待填模板
    python scripts/preflight.py --config-dir config --outdir out # 额外生成 待补充输入.md

退出码:
    0 = 可以开工
    2 = 缺必需输入，禁止开工
    1 = 有警告，人工确认后可开工
"""

import argparse
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
EXAMPLE_DIR = os.path.join(SKILL_ROOT, "config")

# file -> (是否必需, 一句话说明, 关键列)
INPUTS = [
    ("requirements.csv", True, "要排期的任务清单", ["名称", "类型"]),
    ("members.csv", True, "可执行的人 / agent 名单", ["账号", "设计组", "状态"]),
    ("workhours.csv", True, "派单规则：类型×规模 → 工时 + 候选组 + 优先级",
     ["类型", "工时", "设计组"]),
    ("schedule.yaml", True, "排期参数：起始日 / deadline / 日工时 / 阈值区间", []),
    ("aliases.csv", False, "需求名 → 标准类型的别名表", ["需求名称", "规则条目"]),
    ("holidays.json", False, "节假日与调休（不填则只跳周末）", []),
]

# 追问话术：缺什么就照着问，别自己编
ASK = {
    "workhours.csv": (
        "把派单规则表给我。每行一条规则，至少 4 列：\n"
        "  1. 类型（礼物 / banner / icon …）\n"
        "  2. 规模区间（价值档位，可留空表示通配）\n"
        "  3. 工时（如 2天 / 4小时）\n"
        "  4. 候选组（可多个，用 / 分隔，如 视觉A组/动效组）\n"
        "  5. 优先级（可选）\n"
        "没有这张表，引擎算不出工作量，也没法决定该给哪个组。"
    ),
    "members.csv": (
        "把成员表给我。每行一个人，至少 3 列：\n"
        "  1. 账号 / 姓名\n"
        "  2. 所属组（要和规则表里的候选组对得上）\n"
        "  3. 状态（在职 / 已离职；离职的不参与排期）\n"
        "可选列：日容量、日成本、类型（human/ai）、并发数。"
    ),
    "requirements.csv": (
        "把要排的需求清单给我。每行一条，至少 2 列：\n"
        "  1. 名称\n"
        "  2. 类型（要和规则表的「类型」列对得上）\n"
        "可选列：规模 / 价值、依赖、指定人、截止。"
    ),
    "schedule.yaml": (
        "确认排期参数：起始日、deadline（或上线日 + 提前天数）、日工时、负载阈值区间。\n"
        "我不会替你猜 deadline——猜错了整张表都偏。"
    ),
}


def norm(path):
    """去掉 BOM 与换行差异后的内容指纹，用于判断"是不是就是模板"。"""
    try:
        with open(path, "rb") as f:
            b = f.read()
    except OSError:
        return None, 0, 0
    b = b.lstrip(b"\xef\xbb\xbf").replace(b"\r\n", b"\n").strip()
    if not b:
        return None, 0, 0
    return hashlib.md5(b).hexdigest(), len(b.splitlines()), b.count(b"\n,") + 1


def head(path):
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            line = f.readline()
        return [c.strip() for c in line.rstrip("\r\n").split(",")]
    except OSError:
        return []


def read_rows(path):
    import csv
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return [r for r in csv.DictReader(f, restkey="_rest") if any(
                (v or "").strip() for k, v in r.items() if k != "_rest")]
    except OSError:
        return []


def check(cfg, name, required, desc, cols):
    p = os.path.join(cfg, name)
    ex = os.path.join(EXAMPLE_DIR, name.replace(".csv", ".example.csv")
                      .replace(".yaml", ".example.yaml")
                      .replace(".json", ".example.json"))
    if not os.path.exists(p):
        return dict(name=name, state="MISSING", required=required, desc=desc,
                    detail="config 里没有这个文件", ask=ASK.get(name, ""))

    h, lines, _ = norm(p)
    if h is None:
        return dict(name=name, state="EMPTY", required=required, desc=desc,
                    detail="文件是空的", ask=ASK.get(name, ""))

    eh, _, _ = norm(ex) if os.path.exists(ex) else (None, 0, 0)
    if eh and h == eh:
        return dict(name=name, state="TEMPLATE", required=required, desc=desc,
                    detail=f"内容和自带的示例模板逐字节相同（{lines} 行），等于没填",
                    ask=ASK.get(name, ""))

    rows = read_rows(p)
    missing_cols = [c for c in cols if c not in (head(p) or [])]
    if missing_cols:
        return dict(name=name, state="BADCOLS", required=required, desc=desc,
                    detail=f"{lines} 行，但缺关键列：{'/'.join(missing_cols)}；"
                           f"实际表头：{'/'.join(head(p)[:8])}",
                    ask=ASK.get(name, ""))

    # 关键列空值率
    blanks = []
    for c in cols:
        if not rows:
            break
        n = sum(1 for r in rows if not (r.get(c) or "").strip())
        if n / len(rows) > 0.3:
            blanks.append(f"{c} 空 {n}/{len(rows)}")
    if blanks:
        return dict(name=name, state="SPARSE", required=required, desc=desc,
                    detail=f"{len(rows)} 行，但有列大面积为空：{'；'.join(blanks)}",
                    ask=ASK.get(name, ""))

    return dict(name=name, state="OK", required=required, desc=desc,
                detail=f"{len(rows)} 行，关键列齐全", ask="")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config-dir", required=True)
    ap.add_argument("--outdir", default=None, help="生成 待补充输入.md 到该目录")
    ap.add_argument("--scaffold", action="store_true",
                    help="把缺失项的示例模板拷进 config 供填写")
    args = ap.parse_args()

    cfg = os.path.abspath(args.config_dir)
    if not os.path.isdir(cfg):
        print(f"!! config 目录不存在：{cfg}")
        return 2

    results = [check(cfg, n, r, d, c) for n, r, d, c in INPUTS]
    blocked = [r for r in results if r["required"] and r["state"] != "OK"]
    warn = [r for r in results if not r["required"] and r["state"] != "OK"]

    print(f"输入校验：{cfg}\n")
    print(f"{'文件':<20}{'状态':<10}{'说明'}")
    print("-" * 78)
    for r in results:
        mark = {"OK": "OK", "MISSING": "缺", "EMPTY": "空", "TEMPLATE": "=模板",
                "BADCOLS": "缺列", "SPARSE": "空太多"}[r["state"]]
        flag = "必需" if r["required"] else "可选"
        print(f"{r['name']:<20}{mark:<10}{r['detail']}  [{flag}]")

    if blocked:
        print("\n" + "!" * 78)
        print("禁止开工：以下必需输入不是你确认过的数据。")
        print("不要从工作区旧产物里推断，不要拿示例模板顶替。直接问用户要：\n")
        for r in blocked:
            print(f"── {r['name']}（{r['desc']}）")
            print(f"   {r['detail']}")
            if r["ask"]:
                print("   " + r["ask"].replace("\n", "\n   "))
            print()
        print("拿到后放进 config/，重跑本脚本。")
        print("!" * 78)

        if args.scaffold:
            for r in blocked:
                src = os.path.join(EXAMPLE_DIR, r["name"].replace(
                    ".csv", ".example.csv").replace(".yaml", ".example.yaml"))
                dst = os.path.join(cfg, r["name"])
                if os.path.exists(src) and not os.path.exists(dst):
                    with open(src, "rb") as f:
                        data = f.read()
                    with open(dst, "wb") as f:
                        f.write(data)
                    print(f"   已拷出待填模板：{dst}")

        if args.outdir:
            os.makedirs(args.outdir, exist_ok=True)
            md = os.path.join(args.outdir, "待补充输入.md")
            with open(md, "w", encoding="utf-8") as f:
                f.write("# 排期待补充输入\n\n")
                for r in blocked:
                    f.write(f"## {r['name']}\n\n{r['desc']}\n\n")
                    if r["ask"]:
                        f.write(r["ask"] + "\n\n")
            print(f"\n待填清单：{md}")
        return 2

    if warn:
        print("\n提示（不阻塞）：")
        for r in warn:
            print(f"  - {r['name']}：{r['detail']}")

    print("\n校验通过，可以跑 plan.py。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
