#!/usr/bin/env python3
"""取中国法定节假日表，存成 plan_assignments.py --holidays 能直接吃的 JSON。

数据源按顺序尝试（第一个成功就用它）：
  1. NateScarlet/holiday-cn 年度 JSON（GitHub raw，含 isOffDay：true=放假日，false=调休上班日）
  2. 同一份数据的 jsDelivr 镜像
  3. timor.tech 节假日接口（只有放假日，没有调休上班日，会提示精度较低）

    python fetch_holidays.py --year 2026 --out .plan/holidays-2026.json

拿不到（离线/被墙）就把公司日历表导出成 CSV（每行一个日期，含「班」字的算调休上班日），
用 --holidays .plan/holidays.csv 传进去。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

SOURCES = [
    ("holiday-cn/github", "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json"),
    ("holiday-cn/jsdelivr", "https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json"),
    ("timor", "https://timor.tech/api/holiday/year/{year}"),
]


def fetch(url: str, timeout: float) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "assign-tasks-to-art-designers/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 固定公开数据源
        return resp.read().decode("utf-8")


def normalize(raw: str, src: str, year: int) -> list[dict]:
    if src.startswith("timor"):
        data = json.loads(raw)
        out = []
        for _, v in (data.get("holiday") or {}).items():
            d = v.get("date") or ""
            if not d:
                continue
            out.append({"name": v.get("name", ""), "date": d, "isOffDay": bool(v.get("holiday"))})
        print("提示：timor 数据不含调休上班日，排期会把调休上班的周末当成休息日", file=sys.stderr)
        return out
    data = json.loads(raw)
    days = data if isinstance(data, list) else data.get("days", [])
    return [{"name": d.get("name", ""), "date": d.get("date", ""), "isOffDay": bool(d.get("isOffDay"))} for d in days]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="下载年度节假日表")
    ap.add_argument("--year", type=int, help="默认取今年的 1 月 1 日所属年份")
    ap.add_argument("--out", required=True, help="输出 .json 或 .csv")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--url", help="自定义数据源（{year} 占位）")
    args = ap.parse_args(argv)

    year = args.year or __import__("datetime").date.today().year
    urls = [(args.url, args.url)] if args.url else SOURCES
    last_err = "没有可用数据源"
    for name, tpl in urls:
        url = tpl.format(year=year)
        try:
            days = normalize(fetch(url, args.timeout), name, year)
        except Exception as exc:  # 网络失败就换下一个源
            last_err = f"{name}: {type(exc).__name__} {exc}"
            print(f"跳过 {name}：{last_err}", file=sys.stderr)
            continue
        if not days:
            last_err = f"{name}: 返回为空"
            continue
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {"year": year, "source": name, "days": days}
        if out.suffix.lower() == ".csv":
            import csv
            with out.open("w", encoding="utf-8-sig", newline="") as fh:
                wr = csv.writer(fh)
                wr.writerow(["date", "name", "isOffDay"])
                for d in days:
                    wr.writerow([d["date"], d["name"], "休" if d["isOffDay"] else "班"])
        else:
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        off = sum(1 for d in days if d["isOffDay"])
        print(f"{year} 年 {len(days)} 条（放假 {off}，调休上班 {len(days) - off}）← {name}\n已写入：{out}")
        return 0
    print(f"所有数据源都失败：{last_err}\n离线请把公司日历导出为 CSV，用 --holidays 传入。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
