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


# 引擎（engine/planner.py）读规则表多余列用的键：sticky 判据「同类型安排同一个人」就从这里取
REST_KEY = "_extra"


def write_csv(path, rows):
    """写 CSV。

    各行列数可能不齐（手工表常见），所以 fieldnames 取所有行的键并集，
    并用 extrasaction=ignore，避免 DictWriter 对多余列抛 ValueError。
    """
    if not rows:
        open(path, "w", encoding="utf-8-sig").write("")
        return
    fields, seen = [], set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                fields.append(k)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(rows)


def read_csv(path):
    """读 CSV。比表头多出来的单元格收进 REST_KEY，不再产生 None 键。

    DictReader 默认把多余列塞到 None 键下：既会让下游写出 None 列，
    也会在「首行不多余、后续行多余」时让 DictWriter 直接崩。
    同时把多余列拼成字符串，保证 sticky 的 `"同一个人" in note` 判定可用。
    """
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, restkey=REST_KEY))
    for r in rows:
        extra = r.get(REST_KEY)
        if isinstance(extra, list):
            r[REST_KEY] = " ".join(str(x) for x in extra if x is not None)
    return rows


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
    if getattr(args, "project_name", ""):
        cmd += ["--doc-name", args.project_name]
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
    ap.add_argument("--project-name", default="",
                    help="需求文档标题，传给引擎 --doc-name（plan.md 里显示为「需求文档」）")
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

    # 3) 求解阈值：全区间扫描，挑收尾最早的解
    deadline, chosen = "", None
    if args.threshold:
        chosen = args.threshold
        run_engine(tmp, args, chosen, outdir)
        deadline = str(sched.get("launch_date", ""))
    elif sched.get("launch_date"):
        ld = datetime.date.fromisoformat(str(sched["launch_date"]))
        n = int(sched.get("finish_before_launch_days", 0))
        deadline = (ld - datetime.timedelta(days=n)).isoformat()
        lo = int(sched.get("max_load_days_min", 1))
        hi = int(sched.get("max_load_days_max", 30))
        # 收尾日期对阈值**不保证单调**：不同阈值下任务会重排到不同人身上，可能出现倒挂。
        # 所以不能二分，必须全区间扫描后挑收尾最早的解；收尾相同时保留更小的阈值（更均衡）。
        best, best_end = None, ""
        for cand in range(lo, hi + 1):
            d = os.path.join(tmp, f"try_{cand}")
            run_engine(tmp, args, cand, d)
            end = plan_end(d)
            if end and (not best_end or end < best_end):
                best, best_end = cand, end
            tag = "" if not end else ("OK" if end <= deadline else "超期")
            print(f"  阈值 {cand} 天 -> 收尾 {end or '无任务'} {tag}")
        chosen = best if best is not None else hi
        if best_end and best_end > deadline:
            print(f"!! 全区间扫描（{lo}~{hi} 天）都压不进 {deadline}；"
                  f"已给出收尾最早的一版（阈值 {chosen} 天 -> {best_end}），需要加人或砍需求。")
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
