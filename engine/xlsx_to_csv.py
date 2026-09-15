#!/usr/bin/env python3
"""Dump every sheet of a local xlsx to one CSV per sheet (UTF-8 with BOM).

Used to (a) read a Tencent Docs sheet the user exported locally, and (b) keep a
snapshot of the online tables so planning can be re-run without network access.

    python xlsx_to_csv.py --src 规则及需求.xlsx --outdir .run
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path


def sheet_to_rows(ws):
    rows = []
    header = None
    for raw in ws.iter_rows(values_only=True):
        cells = ["" if c is None else str(c).strip() for c in raw]
        while cells and cells[-1] == "":
            cells.pop()
        if not cells:
            continue
        if header is None:
            header = cells
            rows.append(header)
            continue
        if len(cells) < len(header):
            cells += [""] * (len(header) - len(cells))
        rows.append(cells)
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="xlsx -> per-sheet CSV")
    ap.add_argument("--src", required=True, help="path to .xlsx")
    ap.add_argument("--outdir", required=True, help="directory to write CSVs into")
    ap.add_argument("--only", nargs="*", default=None, help="only these sheet names")
    args = ap.parse_args(argv)

    try:
        import openpyxl
    except ImportError:  # pragma: no cover
        print("需要 openpyxl：pip install openpyxl", file=sys.stderr)
        return 2

    src = Path(args.src)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    # 不用 read_only：部分导出文件的 <dimension> 声明不完整，只读模式会少读列
    wb = openpyxl.load_workbook(src, data_only=True)
    written = []
    for ws in wb.worksheets:
        if args.only and ws.title not in args.only:
            continue
        rows = sheet_to_rows(ws)
        if not rows:
            continue
        safe = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", ws.title)
        dest = outdir / f"{safe}.csv"
        with dest.open("w", encoding="utf-8-sig", newline="") as fh:
            csv.writer(fh).writerows(rows)
        written.append((ws.title, dest, len(rows) - 1))

    for title, dest, n in written:
        print(f"{title}\t{n} 行\t{dest}")
    if not written:
        print("没有可导出的工作表", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
