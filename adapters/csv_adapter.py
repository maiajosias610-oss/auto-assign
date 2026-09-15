"""CSV 适配器：不开飞书也能跑完整流程（导入 / 预览 / 导出写回指令）。"""
import csv
import datetime


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fieldnames=None):
    if not rows:
        return
    fieldnames = fieldnames or list(rows[0].keys())
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def d2ms(d):
    return int(datetime.datetime.strptime(d, "%Y-%m-%d").timestamp() * 1000)


def ms2d(v):
    return datetime.datetime.fromtimestamp(int(v) / 1000).strftime("%Y-%m-%d") if v else ""


class CsvBackend:
    """把 CSV 伪装成最小可读写后端：只有 read / export_updates，没有在线写。"""

    def __init__(self, outdir):
        self.outdir = outdir

    def read(self, name):
        import os
        p = os.path.join(self.outdir, f"{name}.csv")
        return read_csv(p) if os.path.exists(p) else []

    def export_updates(self, updates, path):
        """updates: [{record_id, fields}] -> 写成一个可直接核对 / 人工导入的 CSV"""
        rows = []
        for u in updates:
            f = u["fields"]
            rows.append({
                "record_id": u["record_id"],
                **{k: v for k, v in f.items()},
            })
        write_csv(path, rows)
        return path
