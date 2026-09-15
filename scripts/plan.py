"""派单编排入口：读配置 -> 拉数据 -> 求解阈值 -> 出预览 -> （可选）写回。

用法：
    python scripts/plan.py --config-dir config --outdir out
    python scripts/plan.py --config-dir config --outdir out --apply
    python scripts/plan.py --config-dir config --outdir out --threshold 12   # 跳过自动求解

飞书模式需要环境变量：FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_APP_TOKEN
                     FEISHU_TABLE_TASK / FEISHU_TABLE_MEMBER
CSV 模式：config/tasks.csv 提供占用数据。
"""
import argparse
import csv
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "adapters"))

ENGINE = os.path.join(ROOT, "engine", "planner.py")
PY = sys.executable


# ---------------- 极简 YAML（只支持两层 key: value，够用且无依赖） ----------------
def load_yaml(path):
    out, cur = {}, None
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            if line.startswith(" ") or line.startswith("\t"):
                k, _, v = line.strip().partition(":")
                if cur is not None:
                    out[cur][k.strip()] = v.strip().strip("'\"")
                continue
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip().strip("'\"")
            if v:
                out[k] = _cast(v)
            else:
                out[k] = {}
                cur = k
    return out


def _cast(v):
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


# ---------------- 列名归一 ----------------
# 引擎（engine/planner.py）认的是这套中文表头；用户表头由 field_map 映射过来
STD = {
    "requirements": {"name": "名称", "type": "类型", "requirement": "设计需求",
                     "value": "价值", "output": "产出位置", "qty": "数量", "requester": "需求方"},
    "members": {"name": "账号", "group": "设计组", "status": "状态",
                "available_from": "可安排时间", "kind": "类型", "daily_capacity": "日容量",
                "model": "模型", "concurrency": "并发数",
                "cost_day": "日成本", "cost_run": "单次成本"},
    "workhours": {"type": "类型", "value": "价值", "hours": "工时",
                  "group": "设计组", "priority": "优先级", "cost": "成本"},
}


def rename_rows(rows, mapping, kind):
    """把用户表头换成引擎标准表头"""
    std = STD[kind]
    rev = {mapping.get(k, s): s for k, s in std.items()}
    out = []
    for r in rows:
        out.append({rev.get(k, k): v for k, v in r.items()})
    return out


def write_csv(path, rows):
    if not rows:
        open(path, "w", encoding="utf-8-sig").write("")
        return
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ---------------- 跑引擎 ----------------
def run_engine(tmp, args, max_load_days, outdir):
    cmd = [PY, ENGINE,
           "--requirements", os.path.join(tmp, "requirements.csv"),
           "--workhours", os.path.join(tmp, "workhours.csv"),
           "--members", os.path.join(tmp, "members.csv"),
           "--project-type", args.project_type,
           "--outdir", outdir,
           "--day-hours", str(args.day_hours),
           "--qty-shape", args.qty_shape]
    if args.today:
        cmd += ["--today", args.today]
    if args.holidays and os.path.exists(args.holidays):
        cmd += ["--holidays", args.holidays]
    if max_load_days:
        cmd += ["--max-load-days", str(max_load_days)]
    if args.alias_map and os.path.exists(args.alias_map):
        cmd += ["--alias-map", args.alias_map]
    if os.path.exists(os.path.join(tmp, "occupancy.csv")):
        cmd += ["--tasks", os.path.join(tmp, "occupancy.csv")]
    if args.write_priority:
        cmd += ["--write-priority"]
    cmd += ["--active-value", args.active_value]
    if args.ai_daily_hours:
        cmd += ["--ai-daily-hours", str(args.ai_daily_hours)]
    if getattr(args, "ai_naming", None):
        cmd += ["--ai-naming", args.ai_naming]
    if getattr(args, "currency", None):
        cmd += ["--currency", str(args.currency)]
    if args.allow_past_start:
        cmd += ["--allow-past-start"]
    if args.work_on_weekends:
        cmd += ["--work-on-weekends"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode not in (0, 4):      # 4 = 有条目待确认，可接受
        raise RuntimeError(f"引擎失败 exit={r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    return r


def plan_end(outdir):
    rows = read_csv(os.path.join(outdir, "tasks.csv"))
    ends = [r["截止时间"] for r in rows if r.get("截止时间")]
    return max(ends) if ends else ""


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config-dir", default=os.path.join(ROOT, "config"))
    ap.add_argument("--outdir", default=os.path.join(ROOT, "out"))
    ap.add_argument("--adapter", choices=["feishu", "csv"], default="csv")
    ap.add_argument("--project-type", default="默认分类")
    ap.add_argument("--project-name", default="")
    ap.add_argument("--threshold", type=float, default=None, help="跳过自动求解，直接指定负载上限（天）")
    ap.add_argument("--apply", action="store_true", help="确认后写回飞书")
    ap.add_argument("--active-value", default=None, help="覆盖成员表「状态」的可派取值")
    ap.add_argument("--ai-daily-hours", type=float, default=None, help="AI 执行者每日容量（小时）")
    ap.add_argument("--ai-naming", choices=["off", "model", "model_task"], default=None,
                    help="AI 展示名：off=成员表名 / model=模型名 / model_task=模型·需求名")
    ap.add_argument("--currency", default=None, help="成本单位符号，默认「元」")
    args = ap.parse_args()

    cfg_dir = args.config_dir
    sched = load_yaml(os.path.join(cfg_dir, "schedule.yaml")) if os.path.exists(
        os.path.join(cfg_dir, "schedule.yaml")) else load_yaml(os.path.join(cfg_dir, "schedule.example.yaml"))
    fmap = load_yaml(os.path.join(cfg_dir, "field_map.yaml")) if os.path.exists(
        os.path.join(cfg_dir, "field_map.yaml")) else load_yaml(os.path.join(cfg_dir, "field_map.example.yaml"))

    for k in ("today", "day_hours", "work_on_weekends", "write_priority",
              "allow_past_start", "qty_shape", "ai_daily_hours", "currency"):
        if k in sched:
            setattr(args, k, sched[k] or getattr(args, k, None))
    # AI 具名方式：命令行 > schedule.yaml > 默认 model_task
    if not getattr(args, "ai_naming", None):
        args.ai_naming = sched.get("ai_naming", "model_task")
    if not args.active_value:
        args.active_value = fmap["members"].get("active_value", "在职")
    args.holidays = os.path.join(cfg_dir, sched.get("holidays", "holidays.json"))
    args.alias_map = os.path.join(cfg_dir, "aliases.csv")
    if not os.path.exists(args.alias_map):
        args.alias_map = os.path.join(cfg_dir, "aliases.example.csv")

    outdir = args.outdir
    os.makedirs(outdir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="dispatch_")

    # 1) 输入归一
    req = rename_rows(read_csv(os.path.join(cfg_dir, "requirements.csv"))
                      if os.path.exists(os.path.join(cfg_dir, "requirements.csv"))
                      else read_csv(os.path.join(cfg_dir, "requirements.example.csv")),
                      fmap["requirements"], "requirements")
    mem = rename_rows(read_csv(os.path.join(cfg_dir, "members.csv"))
                      if os.path.exists(os.path.join(cfg_dir, "members.csv"))
                      else read_csv(os.path.join(cfg_dir, "members.example.csv")),
                      fmap["members"], "members")
    wh = rename_rows(read_csv(os.path.join(cfg_dir, "workhours.csv"))
                     if os.path.exists(os.path.join(cfg_dir, "workhours.csv"))
                     else read_csv(os.path.join(cfg_dir, "workhours.example.csv")),
                     fmap["workhours"], "workhours")
    write_csv(os.path.join(tmp, "requirements.csv"), req)
    write_csv(os.path.join(tmp, "members.csv"), mem)
    write_csv(os.path.join(tmp, "workhours.csv"), wh)

    # 2) 占用（排除本项目自身 + 排除已完成 + 必须有截止时间）
    if args.adapter == "feishu":
        from feishu_adapter import Feishu, txt, ms2d, link_ids
        fs = Feishu()
        tm = fmap["tasks"]
        member_ids = {r["record_id"]: txt(r["fields"].get(tm["name"]))
                      for r in fs.search("member")}
        cur = fs.search("task", {"conjunction": "and", "conditions": [
            {"field_name": tm["status"], "operator": "isNot", "value": [tm["done_value"]]},
            {"field_name": tm["end"], "operator": "isNotEmpty", "value": []}]})
        rows = []
        for r in cur:
            ids = link_ids(r["fields"].get(tm["designer"]))
            nm = member_ids.get(ids[0], "") if ids else ""
            if nm:
                rows.append({"设计师": nm, "开始时间": ms2d(r["fields"].get(tm["start"])),
                             "截止时间": ms2d(r["fields"].get(tm["end"])),
                             "状态": r["fields"].get(tm["status"]) or ""})
        write_csv(os.path.join(tmp, "occupancy.csv"), rows)
        print(f"飞书占用：{len(rows)} 条")
    elif os.path.exists(os.path.join(cfg_dir, "tasks.csv")):
        shutil.copy(os.path.join(cfg_dir, "tasks.csv"), os.path.join(tmp, "occupancy.csv"))

    # 3) 求解阈值：二分找满足 deadline 的最大阈值
    deadline, chosen = "", None
    if args.threshold:
        chosen = args.threshold
        run_engine(tmp, args, chosen, outdir)
        deadline = str(sched.get("launch_date", ""))
    elif sched.get("launch_date"):
        ld = datetime.date.fromisoformat(str(sched["launch_date"]))
        n = int(sched.get("finish_before_launch_days", 0))
        deadline = (ld - datetime.timedelta(days=n)).isoformat()
        lo, hi = int(sched.get("max_load_days_min", 1)), int(sched.get("max_load_days_max", 30))
        # 单调性：阈值越小 -> 摊得越开 -> 收尾越早。故二分找「满足 deadline 的最小阈值」= 最均衡解
        run_engine(tmp, args, hi, os.path.join(tmp, "try_hi"))
        if plan_end(os.path.join(tmp, "try_hi")) > deadline:
            print(f"!! 最放宽（阈值 {hi} 天）也压不进 {deadline}，需要加人或砍需求。")
            print("   以下按最放宽阈值出方案，请人工决策。")
            best = hi
        else:
            best = hi
            while lo <= hi:
                mid = (lo + hi) // 2
                run_engine(tmp, args, mid, os.path.join(tmp, f"try_{mid}"))
                end = plan_end(os.path.join(tmp, f"try_{mid}"))
                ok = end <= deadline
                print(f"  阈值 {mid} 天 -> 收尾 {end} {'OK' if ok else '超期'}")
                if ok:
                    best, hi = mid, mid - 1
                else:
                    lo = mid + 1
        chosen = best
        run_engine(tmp, args, chosen, outdir)
    else:
        run_engine(tmp, args, None, outdir)

    end = plan_end(outdir)
    print(f"\n阈值 = {chosen} 天 | deadline = {deadline or '未设上线日'} | 实际收尾 = {end}")
    print(f"预览：{outdir}/plan.md  {outdir}/tasks.csv")

    # 4) 写回
    if not args.apply:
        print("（预览模式，加 --apply 写回）")
        return
    if args.adapter != "feishu":
        print("CSV 适配器无在线写回，已导出 update_payload.json 供人工导入")
        return
    from feishu_adapter import Feishu, txt, link_ids, d2ms
    fs = Feishu()
    tm = fmap["tasks"]
    cur = fs.search("task")
    by_name = {txt(r["fields"].get(tm["name"])): r["record_id"] for r in cur}
    member_ids = {txt(r["fields"].get(tm["name"])): r["record_id"] for r in fs.search("member")}
    ok, fail = 0, []
    for r in read_csv(os.path.join(outdir, "tasks.csv")):
        rid = by_name.get(r["名称"])
        if not rid:
            fail.append((r["名称"], "未找到 record_id"))
            continue
        fs.update("task", rid, {
            tm["designer"]: [member_ids[r["设计师"]]],
            tm["start"]: d2ms(r["开始时间"]),
            tm["end"]: d2ms(r["截止时间"]),
        })
        ok += 1
    print(f"写回完成 ok={ok} fail={len(fail)}")
    for f in fail:
        print("  !!", f)


if __name__ == "__main__":
    main()
