#!/usr/bin/env python3
"""Deterministic planner for 美术设计任务自动派单 (assign-tasks-to-art-designers).

读三张表（CSV）→ 算工时/容量排期/挑设计师 → 输出待确认的派单方案。
本脚本不联网、不写任何在线文档；写回是 agent 在用户确认后的事。

口径（用户 2026-08-25 确认，改前先问）：
  · 1 人 1 天 = 8 小时；0.5 天 = 4 小时；「2小时」= 0.25 天
  · 半天及以内可同人同日串排；超过半天必须占空整天
  · 数量 N 默认拆成 N 条子任务 + 1 条主任务（形状 C，任务表「父记录」字段做父子）
  · 规则表「类型」列就是条目名（礼物/头像框/…），与需求表「名称」列匹配；「设计组」列是
    多值优先链（视觉A组/视觉B组//UI组/动效组），按顺序找第一个有人的组
  · 规则表 P0–P38 只决定排期顺序；任务表「优先级」列（P0–P3）是需求紧急度，默认不写
  · 只派成员表状态明确写「在职」的人
  · 价值 1 元 = 100 蓝钻；规则表价值区间是蓝钻时先换算成元再比
  · 备注列（无表头）含「同一个人」时，同一条规则行的任务尽量续派同一人

退出码：0 可直接执行 / 4 有人工确认项 / 3 有任务算不出工时 / 2 输入不可用
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ------------------------------------------------------------------ 列名容错

ALIASES = {
    "name": ["名称", "任务名称", "设计名称", "需求名称", "标题", "title"],
    "type": ["类型", "任务类型", "设计类型", "需求类型"],
    "requirement": ["设计需求", "需求", "需求描述", "描述", "说明"],
    "value": ["价值", "蓝钻", "价格", "价值（元）", "定价"],
    "output": ["产出位置", "产出", "备注/产出", "备注", "交付位置"],
    "qty": ["数量", "份数", "个数", "数量（个）"],
    "requester": ["需求方", "提出人"],
    "hours": ["工时", "计划工时", "工时（天）", "人天", "天数", "所需工时"],
    "priority": ["优先级", "优先等级", "priority"],
    "group": ["设计组", "组", "所属组", "任务负责人", "负责人", "团队", "岗位"],
    "member": ["成员", "姓名", "设计师", "名字", "人员", "name", "账号"],
    "free_from": ["可安排时间", "最后任务截止时间", "最后截止时间", "可用时间", "可用日期", "空闲日期"],
    "status": ["状态", "在职状态", "人员状态"],
    "project_name": ["项目名称", "项目", "name"],
    "project_type": ["项目类型", "类型"],
    "prefix": ["前缀", "项目类型前缀", "编号前缀", "标题前缀"],
    "designer": ["设计师", "负责人", "执行人", "任务执行人", "成员"],
    "start": ["开始时间", "开始日期"],
    "end": ["截止时间", "结束时间", "截止日期", "计划完成时间"],
    # 执行者类型与自定义日容量（人机混排用）
    "member_kind": ["类型", "执行者类型", "资源类型", "kind", "type"],
    "daily_capacity": ["日容量", "每日容量", "日产能", "daily_capacity"],
    # 人机混排成本（元）
    "cost_day": ["日成本", "人天成本", "元/天", "日单价", "成本/天", "日薪", "daily_cost"],
    "cost_run": ["单次成本", "任务成本", "元/次", "单次价格", "调用成本", "run_cost"],
    "rule_cost": ["单次成本", "成本", "元/次", "调用成本", "单位成本", "cost"],
    # AI 计费模式：按次（单次成本）或 订阅（订阅费 ÷ 预估次数 分摊）
    "billing_mode": ["计费模式", "计费方式", "结算方式", "billing"],
    "sub_fee": ["订阅费用", "订阅费", "月费", "包月费", "套餐费", "月租", "subscription_fee"],
    "sub_period": ["订阅周期", "计费周期", "结算周期", "周期", "period"],
    "sub_volume": ["预估次数", "月预估次数", "周期预估量", "预计调用次数", "月调用次数", "estimated_calls"],
    "sub_runs": ["本月已跑次数", "已跑次数", "当月调用次数", "累计次数", "本月调用", "runs_this_month"],
    # AI 具名实例
    "model": ["模型", "模型名", "model", "模型/版本"],
    "concurrency": ["并发数", "并发", "并行数", "实例数", "concurrency"],
}

# 「状态」列里表示可用（可派）的取值，可由 --active-value 覆盖
ACTIVE_VALUE = "在职"

WILD = {"", "/", "-", "—", "–", "无", "不限", "任意", "n/a", "na", "none", "."}
DONE_WORDS = r"已完成|超时完成|已交付|已关单|关单|取消|作废|done|closed"
LUANZUAN = r"蓝钻|钻|钻石|diamond"
YUAN = r"元|块|¥|￥|cny|rmb"

GROUP_SYNONYMS = [
    {"视觉", "原画", "平面"},
    {"动效", "特效", "动画"},
    {"ui", "界面", "交互"},
]


def norm_key(s) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower().replace("（", "(").replace("）", ")")


def cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def read_csv(path: Path) -> list[dict]:
    """DictReader + restkey：无表头的多余列（如规则表的备注）进 _extra。"""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh, restkey="_rest"))
    out = []
    for r in rows:
        rec = {norm_key(k): cell(v) for k, v in r.items() if k is not None and k != "_rest"}
        extra = [cell(v) for v in (r.get("_rest") or []) if cell(v)]
        if extra:
            rec["_extra"] = "，".join(extra)
        if any(rec.values()):
            out.append(rec)
    return out


def col(rows: list[dict], field: str) -> str | None:
    if not rows:
        return None
    wanted = [norm_key(a) for a in ALIASES[field]]
    keys = list(rows[0].keys())
    for w in wanted:
        for k in keys:
            if k == w:
                return k
    for w in wanted:
        if len(w) < 2:
            continue
        for k in keys:
            if w in k:
                return k
    return None


def get(row: dict, key: str | None) -> str:
    return row.get(key, "") if key else ""


# ------------------------------------------------------------------ 值解析

NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def first_number(text) -> float | None:
    m = NUM_RE.search(str(text or "").replace(",", ""))
    return float(m.group()) if m else None


def money_to_yuan(text, rate: float) -> tuple[float | None, str]:
    s = str(text or "").strip()
    if s.lower() in WILD:
        return None, "无价值"
    n = first_number(s)
    if n is None:
        return None, "无价值"
    if re.search(r"经验|积分", s):
        return n / rate, f"{n:g}{re.search(r'经验|积分', s).group()}按1:1折蓝钻÷{rate:g}={n / rate:g}元"  # 用户确认 1经验=1蓝钻
    if re.search(LUANZUAN, s, re.I) and not re.search(YUAN, s, re.I):
        return n / rate, f"{n:g}蓝钻÷{rate:g}={n / rate:g}元"
    return n, f"{n:g}元"


def parse_qty(text) -> int:
    s = str(text or "")
    m = re.search(r"[*xX×]\s*(\d+)", s) or re.search(r"[（(]\s*(\d+)\s*[个件张条套份]?[)）]", s)
    return int(m.group(1)) if m else 0


def clean_type(s) -> str:
    return re.sub(r"\s*[*xX×]\s*\d+\s*", "", str(s or "")).strip()


def parse_days(text) -> float | None:
    """`1天`=1，`4小时`=0.5，`2小时`=0.25，`1周`=5，纯数字按天。"""
    s = str(text or "")
    n = first_number(s)
    if n is None:
        return None
    if "小时" in s or re.search(r"\bh(r)?\b", s, re.I):
        return n / 8.0
    if "周" in s:
        return n * 5.0
    return n


def _money(text) -> float | None:
    """`3.5` / `¥3.5` / `3.5元` / `0.02` → float；空或不可解析 → None。"""
    s = str(text or "").strip().replace(",", "")
    if not s or s.lower() in WILD:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def parse_priority(text) -> tuple[int, bool]:
    s = str(text or "").strip()
    m = re.search(r"[pP]\s*(\d+)", s) or re.fullmatch(r"(\d+)", s)
    if m:
        return int(m.group(1)), True
    for word, rank in (("高", 0), ("中", 1), ("低", 2)):
        if word in s:
            return rank, True
    return 10**6, False


RANGE_SEP = re.compile(r"\s*(?:~|～|至|到|[-–—])\s*")


def parse_value_rule(text):
    s = re.sub(r"\s+", "", str(text or ""))
    if s.lower() in WILD:
        return {"kind": "wild", "raw": text}
    nums = [float(x) for x in NUM_RE.findall(s.replace(",", ""))]
    if not nums:
        return {"kind": "wild", "raw": text}
    has_range = bool(RANGE_SEP.search(s))
    if not has_range and any(t in s for t in ("以上", "及以上", "≥", ">=", ">", "+")):
        return {"kind": "min", "lo": nums[0], "raw": text}
    if not has_range and any(t in s for t in ("以下", "及以下", "≤", "<=", "<")):
        return {"kind": "max", "hi": nums[-1], "raw": text}
    if has_range and len(nums) >= 2:
        return {"kind": "range", "lo": min(nums[0], nums[-1]), "hi": max(nums[0], nums[-1]), "raw": text}
    return {"kind": "exact", "lo": nums[0], "hi": nums[0], "raw": text}


def value_matches(rule: dict, value: float | None) -> bool:
    if rule["kind"] == "wild":
        return True
    if value is None:
        return False
    eps = 1e-9
    if rule["kind"] == "min":
        return value >= rule["lo"] - eps
    if rule["kind"] == "max":
        return value <= rule["hi"] + eps
    return rule["lo"] - eps <= value <= rule["hi"] + eps


def parse_date(text, fallback: date, tz_offset=8) -> tuple[date, bool]:
    s = str(text or "").strip()
    if not s:
        return fallback, False
    if re.fullmatch(r"\d{12,13}", s):  # 飞书日期字段的毫秒时间戳
        ts = int(s) / 1000 if len(s) == 13 else int(s)
        return datetime.fromtimestamp(ts, timezone(timedelta(hours=tz_offset))).date(), True
    s = s.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-").replace(".", "-")
    s = s.split("T")[0].split(" ")[0]
    parts = [p for p in re.split(r"-+", s) if p]
    try:
        if len(parts) == 3:
            return date(int(parts[0]), int(parts[1]), int(parts[2])), True
        if len(parts) == 2:
            return date(fallback.year, int(parts[0]), int(parts[1])), True
        if len(parts) == 1 and re.fullmatch(r"\d{8}", parts[0]):
            d = parts[0]
            return date(int(d[:4]), int(d[4:6]), int(d[6:])), True
    except ValueError:
        return fallback, False
    return fallback, False


def to_ms(d: date, tz_offset: int) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone(timedelta(hours=tz_offset))).timestamp() * 1000)


def load_holidays(spec: str | None, notes: list) -> tuple[set, set]:
    """返回 (放假日, 调休上班日)。支持 holiday-cn 的 JSON 或每行一个日期的 CSV/TXT。"""
    off, work = set(), set()
    if not spec:
        return off, work
    p = Path(spec)
    if not p.exists():
        notes.append(f"节假日文件不存在：{spec}，按只跳周末处理")
        return off, work
    txt = p.read_text(encoding="utf-8-sig")
    if p.suffix.lower() == ".json" or txt.lstrip()[:1] in "[{":
        data = json.loads(txt)
        items = data if isinstance(data, list) else data.get("days", [])
        for d in items:
            if not isinstance(d, dict):
                continue
            ds = str(d.get("date", ""))[:10]
            try:
                y, m, dd = ds.split("-")
                day = date(int(y), int(m), int(dd))
            except ValueError:
                continue
            (off if d.get("isOffDay", True) else work).add(day)
    else:
        for line in txt.splitlines():
            line = line.strip().strip(",")
            if not line or norm_key(line) in {"date", "日期", "holiday", "节假日"}:
                continue
            for tok in re.findall(r"\d{4}-\d{2}-\d{2}", line):
                y, m, dd = tok.split("-")
                day = date(int(y), int(m), int(dd))
                (work if "班" in line else off).add(day)
    return off, work


# ------------------------------------------------------------------ 规则匹配

def group_key(g: str) -> str:
    s = norm_key(g)
    for i, syn in enumerate(GROUP_SYNONYMS):
        if any(w in s for w in syn):
            return f"#{i}"
    return s


def same_group(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return norm_key(a) == norm_key(b) or group_key(a) == group_key(b)


def _name_hits(rows, nkey, fuzzy):
    if not nkey:
        return []
    if not fuzzy:
        return [r for r in rows if norm_key(r["task_name"]) == nkey]
    return [r for r in rows if norm_key(r["task_name"]) and (norm_key(r["task_name"]) in nkey or nkey in norm_key(r["task_name"]))]


def _pick_pool(rows, value):
    ex = [r for r in rows if r["rule"]["kind"] != "wild" and value_matches(r["rule"], value)]
    if ex:
        return ex, "+价值区间"
    wild = [r for r in rows if r["rule"]["kind"] == "wild"]
    if wild:
        return wild, "+通配行(价值=/)"
    return None, ""


def match_rule(clean_type: str, value_yuan: float | None, name: str, rules: list[dict]):
    """定位规则行。新契约里规则表「类型」列就是条目名，所以名称匹配是主路径：

    条目名精确（头像框）→ 条目名模糊（小型礼物→礼物，需确认）→ 需求类型匹配（旧表）→ 价值/通配。
    同一条目多档价值（礼物的 5 档蓝钻区间）由价值区间收敛，不按优先级抢。
    """
    warns, needs_confirm = [], False
    rtype, nkey = norm_key(clean_type), norm_key(name)
    value = first_number(str(value_yuan or "")) if value_yuan is not None else None
    same_type = [r for r in rules if r["type"] == rtype] if rtype else []
    others = [r for r in rules if r["type"] != rtype]

    tiers = []
    if same_type:
        tiers.append(("类型+任务名称", _name_hits(same_type, nkey, False), False))
        tiers.append(("类型+任务名称(模糊)", _name_hits(same_type, nkey, True), False))
    tiers.append(("条目名精确", _name_hits(rules, nkey, False), False))
    tiers.append(("条目名模糊", _name_hits(rules, nkey, True), True))
    if same_type:
        tiers.append(("类型", same_type, True))

    for via, rows_, soft in tiers:
        if not rows_:
            continue
        pool, how = _pick_pool(rows_, value)
        if pool is None:
            continue
        via += how
        needs_confirm = soft or len(pool) > 1
        if soft:
            warns.append(f"需求名「{name}」没有完全相同的规则条目，模糊命中「{pool[0]['task_name']}」（{via}）——请确认是不是同一个东西")
        if len(pool) > 1:
            detail = "；".join(f"{r['task_name']}/{r['priority_raw']}/{r['hours_raw']}" for r in pool[:4])
            warns.append(f"{len(pool)} 条规则同时命中（{detail}），取优先级最高的一条——建议核对规则表")
            needs_confirm = True
        return pool[0], via, warns, needs_confirm

    known = "、".join(sorted({r["task_name"] for r in rules if r["task_name"]})[:20])
    warns.append(f"需求「{name}」（类型「{clean_type}」、价值 {value_yuan}）在规则表里找不到条目（现有条目如：{known}…）")
    return None, "", warns, True


# ------------------------------------------------------------------ 容量排期

class Calendar:
    def __init__(self, off: set, work: set, always: bool = False):
        self.off, self.work, self.always = off, work, always

    def is_workday(self, d: date) -> bool:
        if self.always:
            return True
        if d in self.work:
            return True
        if d in self.off:
            return False
        return d.weekday() < 5

    def open_day(self, d: date) -> date:
        while not self.is_workday(d):
            d += timedelta(days=1)
        return d


def ai_display_name(a: dict, task_name: str, mode: str) -> str:
    """AI 执行者的展示名。

    off        → 成员表里的名字（如 初稿Agent）
    model      → 模型名（如 claude-sonnet-4）
    model_task → 模型·需求名（如 claude-sonnet-4·首页主banner），同一模型并行跑多任务时可读
    """
    if a.get("kind") != "ai" or mode == "off":
        return a.get("label") or a.get("name", "")
    model = (a.get("model") or "").strip() or a.get("label") or a.get("name", "")
    if mode == "model":
        return model
    return f"{model}·{task_name}"


def task_cost(rule, a: dict, hours, day_hours: float) -> tuple[float | None, str]:
    """单条任务成本。优先级：规则表单次成本 > AI 单次成本 > 按日成本折算。

    人：日成本 × 工时(天)；AI：单次成本（按调用计费，与耗时无关）或按容量折算。
    """
    if rule and rule.get("cost") is not None:
        return round(rule["cost"], 4), "规则表单次成本"
    if a.get("kind") == "ai":
        if a.get("cost_run") is not None:
            return round(a["cost_run"], 4), a.get("cost_run_note") or "AI 单次调用成本"
        if a.get("cost_day") is not None:
            dh = a.get("dh") or day_hours
            return round(a["cost_day"] * (hours or 0) / dh, 4), "AI 按日成本折算"
        return None, ""
    if a.get("cost_day") is not None:
        return round(a["cost_day"] * (hours or 0) / day_hours, 4), "人天成本"
    return None, ""


class Scheduler:
    """每人每个工作日 day_hours 小时容量。

    ≤ 半天：塞当天空隙（同人同日串排）；> 半天：必须从空整天起铺。
    规则行的「设计组」是多值优先链：按顺序找第一个有可用成员的组。
    """

    def __init__(self, members, cal: Calendar, day_hours: float, cursor0: date, respect_today: bool,
                 max_load_days: float | None = None):
        self.cal = cal
        self.day_hours = day_hours
        self.half = day_hours / 2
        self.max_load_days = max_load_days
        self.max_load_h = max_load_days * day_hours if max_load_days else None
        self.members = {}
        for m in members:
            cur = m["free_from"]
            if respect_today:
                cur = max(cur, cursor0)
            mcal = m.get("cal") or cal                    # AI 可用全周无休日历
            mdh = m.get("daily_hours") or day_hours       # AI 可自定义日容量
            self.members[m["name"]] = {
                "name": m["name"], "group": m["group"], "active": m["active"],
                "kind": m.get("kind", "human"), "cal": mcal, "dh": mdh,
                "used": dict(m["busy"]), "cursor": mcal.open_day(cur), "load": 0.0,
                "free_raw": m["free_raw"], "busy_span": m["busy_span"],
                "slot": m.get("slot", m["name"]), "label": m.get("label", m["name"]),
                "model": m.get("model", ""), "cost_day": m.get("cost_day"),
                "cost_run": m.get("cost_run"), "cost_run_note": m.get("cost_run_note", ""),
            }

    def _scan(self, m, hours: float) -> date | None:
        cal_, dh_, half_ = m["cal"], m["dh"], m["dh"] / 2
        d, guard, eps = m["cursor"], 0, 1e-9
        while guard < 800:
            guard += 1
            d = cal_.open_day(d)
            used = m["used"].get(d, 0.0)
            if hours <= half_ + eps:
                if dh_ - used >= hours - eps:
                    return d
                d += timedelta(days=1)
                continue
            if used > eps:
                d += timedelta(days=1)
                continue
            left, dd, fits = hours, d, True
            while left > eps:
                dd = cal_.open_day(dd)
                if m["used"].get(dd, 0.0) > eps:
                    fits = False
                    break
                left -= min(left, dh_)
                dd += timedelta(days=1)
            if fits:
                return d
            d = dd
        return None

    def _place(self, m, hours: float, start: date) -> list[date]:
        cal_, dh_, half_ = m["cal"], m["dh"], m["dh"] / 2
        eps, d, left, days = 1e-9, start, hours, []
        while left > eps:
            d = cal_.open_day(d)
            used = m["used"].get(d, 0.0)
            if hours > half_ + eps and used > eps:
                d += timedelta(days=1)
                continue
            if dh_ - used <= eps:
                d += timedelta(days=1)
                continue
            take = min(left, dh_ - used)
            m["used"][d] = used + take
            days.append(d)
            left -= take
            d += timedelta(days=1)
        return days

    def _commit(self, m, start: date, hours: float, note: str = ""):
        days = self._place(m, hours, start)
        m["load"] += hours
        m["cursor"] = days[-1]
        return {"name": m["name"], "group": m["group"], "start": days[0], "end": days[-1],
                "workdays": len(days), "note": note,
                "kind": m.get("kind", "human"), "slot": m.get("slot", m["name"]),
                "label": m.get("label", m["name"]), "model": m.get("model", ""),
                "cost_day": m.get("cost_day"), "cost_run": m.get("cost_run"),
                "cost_run_note": m.get("cost_run_note", ""),
                "dh": m.get("dh")}

    def _under_cap(self, m, hours: float) -> bool:
        if not self.max_load_h:
            return True
        return m["load"] + hours <= self.max_load_h + 1e-9

    def pick(self, groups: list[str], hours: float, sticky: str | None = None):
        pool_all = [m for m in self.members.values() if m["active"]]
        if not pool_all:
            return None, "成员表里没有在职成员"
        cap_txt = f"单人负载上限 {self.max_load_days:g} 天" if self.max_load_h else ""
        chain = [g for g in (groups or []) if g and g.lower() not in WILD]
        if sticky:  # 规则备注「同类型安排同一个人」：原人排得下就续用
            m = self.members.get(sticky)
            if m and m["active"]:
                d = self._scan(m, hours)
                if d is not None:
                    return self._commit(m, d, hours, note=f"同类型续用 {sticky}"), ""
        spill = ""
        for g in chain:
            pool = [m for m in pool_all if same_group(m["group"], g)] if g else pool_all
            scored = [(self._scan(m, hours), m) for m in pool]
            scored = [(d, m) for d, m in scored if d is not None]
            if not scored:
                continue
            under = [(d, m) for d, m in scored if self._under_cap(m, hours)]
            if under:
                under.sort(key=lambda x: (x[0], round(x[1]["load"], 2), x[1]["name"]))
                return self._commit(under[0][1], under[0][0], hours), spill
            if self.max_load_h:
                spill = f"{cap_txt}：组「{g}」可用人选均已满载，按负载均衡顺延到下一组"
        scored = []
        for m in pool_all:
            d = self._scan(m, hours)
            if d is not None:
                scored.append((d, m))
        if not scored:
            return None, "所有在职成员都排不下（工时或日历有误？）"
        under = [(d, m) for d, m in scored if self._under_cap(m, hours)] if self.max_load_h else []
        cand = under or scored
        cand.sort(key=lambda x: (x[0], round(x[1]["load"], 2), x[1]["name"]))
        if self.max_load_h and chain:
            warn = (f"{cap_txt}：规则链 {'→'.join(chain)} 全员满载，"
                    f"从{'全员未满载者' if under else '全员'}里挑了空闲最早的人")
        else:
            warn = f"设计组 {'→'.join(chain) or '(空)'} 都没有可用成员，从全员里挑了人"
        return self._commit(cand[0][1], cand[0][0], hours), warn


# ------------------------------------------------------------------ 载入

def load_workhours(path, rate: float):
    rules, group_order, warns = [], [], []
    rows = read_csv(Path(path))
    if not rows:
        return rules, group_order, [f"「类型工时」表为空或缺失：{path}"]
    k = {f: col(rows, f) for f in ("type", "value", "hours", "priority", "group", "name", "rule_cost")}
    if not k["type"]:
        return rules, group_order, ["「类型工时」表缺少「类型」列"]
    prio_ok = True
    for i, r in enumerate(rows):
        t_raw = get(r, k["type"])
        groups = [s.strip() for s in re.split(r"[/、,，;；]", get(r, k["group"]))
                  if s.strip() and s.strip().lower() not in WILD]
        for g in groups:
            if g not in group_order:
                group_order.append(g)
        prio, ok = parse_priority(get(r, k["priority"]))
        prio_ok = prio_ok and ok
        raw_val = get(r, k["value"])
        rule = parse_value_rule(raw_val)
        if rule["kind"] != "wild" and re.search(LUANZUAN, raw_val or "", re.I) and not re.search(YUAN, raw_val or "", re.I):
            for key in ("lo", "hi"):
                if rule.get(key) is not None:
                    rule[key] = rule[key] / rate
        note = get(r, "_extra")
        rules.append({
            "row": i + 2,
            "task_name": get(r, k["name"]) or t_raw,  # 新契约：类型列就是条目名
            "type_raw": t_raw,
            "type": norm_key(clean_type(t_raw)),
            "rule": rule,
            "days": parse_days(get(r, k["hours"])),
            "hours_raw": get(r, k["hours"]),
            "priority": prio, "priority_raw": get(r, k["priority"]),
            "group": groups[0] if groups else "",
            "groups": groups,
            "sticky": "同一个人" in note,
            "note": note,
            "cost": _money(get(r, k["rule_cost"])) if k["rule_cost"] else None,
        })
    if not prio_ok:
        warns.append("「优先级」列存在无法识别的值（期望 P0/P1/…），未识别的排在最后")
    rules.sort(key=lambda r: (r["priority"], r["row"]))
    return rules, group_order, warns


def load_members(mpath, tpath, today: date, tz: int):
    warns = []
    rows = read_csv(Path(mpath)) if mpath else []
    if not rows:
        return [], ["成员表缺失或为空，无法分派设计师"]
    k = {f: col(rows, f) for f in ("member", "group", "status", "free_from")}
    busy: dict[str, dict[date, float]] = {}
    span: dict[str, tuple] = {}
    if tpath and Path(tpath).exists():
        trows = read_csv(Path(tpath))
        tk = {f: col(trows, f) for f in ("designer", "start", "end", "status")}
        for t in trows:
            raw_who = get(t, tk["designer"])
            if not raw_who:
                continue
            if re.search(DONE_WORDS, get(t, tk["status"]), re.I):
                continue
            d1, ok1 = parse_date(get(t, tk["start"]), today, tz)
            d2, ok2 = parse_date(get(t, tk["end"]), today, tz)
            if not ok2:
                continue
            d1 = d1 if ok1 and d1 < d2 else d2
            for who in re.split(r"[、,，;；/]", raw_who):
                who = who.strip()
                if not who:
                    continue
                days = busy.setdefault(who, {})
                for d in [d1 + timedelta(days=n) for n in range((d2 - d1).days + 1)]:
                    if d.weekday() < 5:
                        days[d] = 8.0
                lo, hi = span.get(who, (d1, d2))
                span[who] = (min(lo, d1), max(hi, d2))

    members = []
    for r in rows:
        name = get(r, k["member"])
        if not name:
            continue
        status = get(r, k["status"])
        # 只派「可用」的执行者：默认认「在职」，可由 --active-value 覆盖（英文 active/available 等）
        active = ACTIVE_VALUE.lower() in status.lower()
        # 执行者类型：human（受工作日/节假日约束）或 ai（默认 7×24，可自定义日容量）
        mk = col(rows, "member_kind")
        kind = (get(r, mk) or "human").strip().lower() if mk else "human"
        mdh = col(rows, "daily_capacity")
        daily = None
        if mdh:
            try:
                daily = float(get(r, mdh))
            except (TypeError, ValueError):
                daily = None
        c_day = _money(get(r, col(rows, "cost_day"))) if col(rows, "cost_day") else None
        c_run = _money(get(r, col(rows, "cost_run"))) if col(rows, "cost_run") else None
        model = (get(r, col(rows, "model")) or "").strip() if col(rows, "model") else ""
        conc = 1
        if col(rows, "concurrency"):
            try:
                conc = max(1, int(float(get(r, col(rows, "concurrency")) or 1)))
            except (TypeError, ValueError):
                conc = 1
        # ---- AI 计费模式：按次（单次成本）或 订阅（订阅费 ÷ 预估次数 分摊） ----
        cost_run_note = ""
        billing_raw = get(r, col(rows, "billing_mode")) if col(rows, "billing_mode") else ""
        billing = norm_key(billing_raw)
        sub_fee = _money(get(r, col(rows, "sub_fee"))) if col(rows, "sub_fee") else None
        sub_vol = _money(get(r, col(rows, "sub_volume"))) if col(rows, "sub_volume") else None
        sub_period_raw = get(r, col(rows, "sub_period")) if col(rows, "sub_period") else ""
        sub_runs = _money(get(r, col(rows, "sub_runs"))) if col(rows, "sub_runs") else None
        is_sub = billing in {"订阅", "订阅制", "包月", "月付", "包年", "年付", "subscription", "monthly", "annual"} \
            or (kind == "ai" and sub_fee and sub_vol and c_run is None)
        if is_sub and sub_fee and sub_vol:
            period_label = norm_key(sub_period_raw) or ("年" if billing in {"包年", "年付", "annual"} else "月")
            c_run = round(sub_fee / sub_vol, 6)
            cost_run_note = f"AI 订阅分摊（{sub_fee:g}/{period_label} ÷ {sub_vol:g}次）"
            billing = billing or "订阅"
        elif c_run is not None:
            billing = billing or "按次"
        raw_free = get(r, k["free_from"])
        b = busy.get(name) or busy.get(norm_key(name)) or {}
        if raw_free:
            free, src = parse_date(raw_free, today, tz)[0], f"「可安排时间」{raw_free}"
        elif b:
            free, src = max(b) + timedelta(days=1), "由未完成任务占用推得"
        else:
            free, src = today, "无在办任务，从今天起"
        sp = span.get(name) or span.get(norm_key(name))
        members.append({"name": name, "group": get(r, k["group"]), "active": active,
                        "kind": kind, "daily_hours": daily,
                        "model": model, "concurrency": conc,
                        "cost_day": c_day, "cost_run": c_run, "cost_run_note": cost_run_note,
                        "billing": billing, "sub_fee": sub_fee, "sub_volume": sub_vol,
                        "sub_runs": sub_runs,
                        "sub_period": period_label if is_sub else "",
                        "free_from": free, "busy": b,
                        "busy_span": f"{sp[0]}~{sp[1]}（{len(b)} 个整天）" if sp else "",
                        "free_raw": src})
    return members, warns


def expand_ai_slots(members):
    """AI 执行者按「并发数」展开成 N 个调度槽：{name}#1..#N。

    槽是真正的调度单位（串行占位），共享同一批成本/模型属性；
    展示名由 --ai-naming 在输出阶段再套娃，不影响排期结果。
    """
    out = []
    for m in members:
        if m.get("kind") != "ai" or int(m.get("concurrency") or 1) <= 1:
            m.setdefault("slot", m["name"])
            m.setdefault("label", m["name"])
            out.append(m)
            continue
        for i in range(1, int(m["concurrency"]) + 1):
            c = dict(m)
            c["name"] = f"{m['name']}#{i}"
            c["slot"] = c["name"]
            c["label"] = m["name"]
            out.append(c)
    return out


def load_projects(path):
    if not path or not Path(path).exists():
        return []
    rows = read_csv(Path(path))
    k = {f: col(rows, f) for f in ("project_name", "project_type")}
    out = []
    for r in rows:
        name = get(r, k["project_name"])
        if name:
            out.append({"name": name, "type": norm_key(get(r, k["project_type"]))})
    return out


def next_prefix(type_key, title_rules, used_letters, warnings):
    """前缀支持两种写法：`C01`（具体值）与 `C01-C99+标题`（区间式，取段内下一个空号）。"""
    if type_key in title_rules:
        p = title_rules[type_key]
        m = re.fullmatch(r"([A-Za-z]+)(\d{1,4})\s*-\s*[A-Za-z]{0,2}\s*(\d{1,4})(?:\s*\+\s*(标题|名称|项目名称)?)?", p)
        if m:
            letter = m.group(1).upper()
            lo, hi = int(m.group(2)), int(m.group(3))
            width = len(m.group(2))
            seq = max(used_letters.get(letter, 0), lo - 1) + 1
            used_letters[letter] = seq
            extra = ""
            if seq > hi:
                warnings.append(f"项目类型「{type_key}」的前缀区间 {letter}{lo:0{width}d}-{letter}{hi:0{width}d} 已用尽，当前给到 {letter}{seq:0{width}d}，请扩充规则表")
                extra = "（已超区间）"
            return (f"{letter}{seq:0{width}d}",
                    f"取自「项目标题规则」{p}：{letter} 段现有最大编号后顺延为 {letter}{seq:0{width}d}{extra}",
                    (letter, seq))
        m2 = re.fullmatch(r"([A-Za-z]+)(\d{1,4})", p)
        if m2:
            letter, num = m2.group(1).upper(), int(m2.group(2))
            seq = max(used_letters.get(letter, 0), num - 1) + 1
            used_letters[letter] = seq
            return f"{letter}{seq:0{len(m2.group(2))}d}", f"取自「项目标题规则」{p}，同字母顺延为 {letter}{seq:0{len(m2.group(2))}d}", (letter, seq)
        return p, f"取自「项目标题规则」{p}（按原样拼接）", None
    letters = [chr(c) for c in range(ord("E"), ord("Z") + 1)]
    letters += [f"{a}{b}" for a in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for b in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
    letter = next((l for l in letters if l not in used_letters), None)
    if letter is None:
        warnings.append("前缀字母已用尽（E→Z→AA→ZZ），请人工指定项目前缀")
        return "", "前缀字母用尽", None
    used_letters[letter] = 1
    return f"{letter}01", f"项目类型未登记在「项目标题规则」，自动分配字母段 {letter}，从 {letter}01 起", (letter, 1)


# ------------------------------------------------------------------ 主流程

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="美术设计任务自动派单：算方案（只算不写）")
    ap.add_argument("--requirements", required=True)
    ap.add_argument("--workhours", required=True)
    ap.add_argument("--title-rules")
    ap.add_argument("--members", required=True)
    ap.add_argument("--tasks", help="已有任务表 CSV：未完成记录整天占位")
    ap.add_argument("--projects")
    ap.add_argument("--project-type", required=True, help="如 VV活动")
    ap.add_argument("--doc-name", help="需求表所在文档标题，缺省用文件名")
    ap.add_argument("--today")
    ap.add_argument("--outdir", default=".plan/plan")
    ap.add_argument("--holidays", help="节假日 JSON/CSV（见 scripts/fetch_holidays.py）")
    ap.add_argument("--day-hours", type=float, default=8.0)
    ap.add_argument("--active-value", default="在职",
                    help="成员表「状态」列里表示可派单的取值（英文表可填 active/available）")
    ap.add_argument("--ai-naming", choices=["off", "model", "model_task"], default="model_task",
                    help="AI 执行者的展示名：off=用成员表名 / model=模型名 / model_task=模型·需求名（默认）")
    ap.add_argument("--currency", default="元", help="成本单位符号，默认「元」")
    ap.add_argument("--ai-daily-hours", type=float, default=None,
                    help="AI 执行者每日容量（小时）；缺省等于 --day-hours。AI 默认 7×24 排期")
    ap.add_argument("--luanzuan-per-yuan", type=float, default=100.0)
    ap.add_argument("--max-load-days", type=float, default=None,
                    help="单人新增负载上限（天）：超过就在本组跳过、顺延到下一组，用于均衡各组忙闲")
    ap.add_argument("--alias-map", help="名称别名 CSV（需求名称,规则条目），用户确认过的映射不再标需确认")
    ap.add_argument("--qty-shape", choices=["parent", "flat", "single"], default="parent",
                    help="数量 N 的形状：parent=1主任务+N子任务（默认，用户已选）；flat=N条平铺（k/N后缀）；single=1条长任务")
    ap.add_argument("--write-priority", action="store_true", help="把规则表的排期档写进任务表「优先级」列（默认不写：那是需求紧急度）")
    ap.add_argument("--allow-past-start", action="store_true")
    ap.add_argument("--work-on-weekends", action="store_true", help="周末也排（不跳；节假日表仍生效）")
    ap.add_argument("--tz-offset", type=int, default=8)
    ap.add_argument("--dump-normalized", action="store_true")
    ap.add_argument("--emit-prefix-update", action="store_true")
    args = ap.parse_args(argv)

    req_path = Path(args.requirements)
    if not req_path.exists():
        print(f"找不到需求表 CSV：{req_path}", file=sys.stderr)
        return 2
    doc_name = args.doc_name or req_path.stem
    today = parse_date(args.today, date.today())[0] if args.today else date.today()
    type_key = norm_key(args.project_type)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    notes: list[str] = []
    off, work = load_holidays(args.holidays, notes)
    warnings += notes
    cal = Calendar(off, work, always=args.work_on_weekends and not (off or work))
    cal_note = (f"工作日排期，节假日 {len(off)} 天 / 调休上班 {len(work)} 天" if (off or work)
                else ("全周无休" if cal.always else "工作日排期（跳过周末，未加载节假日表）"))

    req_rows = read_csv(req_path)
    if not req_rows:
        print(f"需求表为空：{req_path}", file=sys.stderr)
        return 2
    rk = {f: col(req_rows, f) for f in ("name", "type", "requirement", "value", "output", "qty", "requester")}
    if not rk["name"]:
        print("需求表缺少「名称」列，无法建任务", file=sys.stderr)
        return 2

    rules, group_order, w = load_workhours(args.workhours, args.luanzuan_per_yuan)
    warnings += w
    global ACTIVE_VALUE
    ACTIVE_VALUE = args.active_value
    members, w = load_members(args.members, args.tasks, today, args.tz_offset)

    # 人机混排：ai 执行者默认 7×24（不受周末/节假日约束），日容量可单独指定
    # 并发数 > 1 时先展开成 N 个调度槽（{name}#1..#N），槽内串行、槽间并行
    members = expand_ai_slots(members)
    cal_ai = Calendar(off, work, always=True)
    n_ai = 0
    for m in members:
        m.setdefault("slot", m["name"])
        m.setdefault("label", m["name"])
        if m.get("kind") == "ai":
            m["cal"] = cal_ai
            m["daily_hours"] = m.get("daily_hours") or args.ai_daily_hours or args.day_hours
    # 按基础 agent 去重统计（槽不算多个 agent）
    n_ai = len({m.get("label", m["name"]) for m in members if m.get("kind") == "ai"})
    n_slot = sum(1 for m in members if m.get("kind") == "ai")
    if n_ai and not cal.always:
        cal_note += f"；其中 {n_ai} 个 AI 执行者（{n_slot} 个并发槽）按 7×24 排期"
    warnings += w
    if not members:
        print("；".join(warnings) or "没有可用成员", file=sys.stderr)
        return 2
    active_n = sum(1 for m in members if m["active"])

    alias_map: dict[str, str] = {}
    if args.alias_map and Path(args.alias_map).exists():
        for r in read_csv(Path(args.alias_map)):
            a, b = get(r, "需求名称"), get(r, "规则条目")
            if a and b:
                alias_map[norm_key(a)] = b

    title_rules = {}
    if args.title_rules and Path(args.title_rules).exists():
        trows = read_csv(Path(args.title_rules))
        tk_type = col(trows, "project_type")
        tk_prefix = col(trows, "prefix")
        for r in trows:
            p = get(r, tk_prefix) or get(r, "_extra")  # 表头只有「项目类型」时，前缀落在无表头列
            t = norm_key(get(r, tk_type))
            if p and t:
                title_rules[t] = p
    else:
        warnings.append("没有「项目标题规则」表：项目类型走自动前缀（E 起）")
    used_letters: dict[str, int] = {}
    for p in title_rules.values():
        m = re.match(r"^([A-Za-z]+)", p)
        if m:
            used_letters.setdefault(m.group(1).upper(), 0)
    projects = load_projects(args.projects)
    for pr in projects:
        m = re.match(r"^([A-Za-z]+)(\d{1,4})", pr["name"])
        if m:
            used_letters[m.group(1).upper()] = max(used_letters.get(m.group(1).upper(), 0), int(m.group(2)))

    # ---- 需求行 → 任务（数量拆分）
    dup_names: dict[str, int] = {}
    for r in req_rows:
        k = norm_key(get(r, rk["name"]))
        dup_names[k] = dup_names.get(k, 0) + 1
    tasks = []
    for i, r in enumerate(req_rows):
        raw_type = get(r, rk["type"])
        ctype = clean_type(raw_type)
        name = get(r, rk["name"])
        # 数量只认裸的 *N 写法；尺寸标注如 进场流光高级特效（650*270）/座驾（600*300）不算数量
        qty_src = re.sub(r"[（(][^（）()]*[)）]", "", str(raw_type or ""))
        qty = parse_qty(qty_src) or parse_qty(name) or int(first_number(get(r, rk["qty"])) or 0)
        yuan, vnote = money_to_yuan(get(r, rk["value"]), args.luanzuan_per_yuan)
        alias = alias_map.get(norm_key(name))
        rule, via, w, confirm = match_rule(ctype, yuan, alias or name, rules)
        if alias and rule:
            via = f"别名「{name}」→「{alias}」｜{via}"
            confirm = False  # 用户已确认的映射不算冲突
        elif alias and not rule:
            w.append(f"别名「{name}」→「{alias}」后仍无规则行")
        warnings += [f"第 {i + 2} 行「{name}」：{x}" for x in w]
        if rule is None:
            warnings.append(f"第 {i + 2} 行「{name}」没有可用规则行 → 不自动派单，需人工决定")
        units = qty if (qty > 1 and args.qty_shape != "single") else 1
        # 同名需求多条时（如 19 行都叫「礼物昵称」），名称追加 价值（产出位置）区分
        base_label = name
        if dup_names.get(norm_key(name), 0) > 1:
            val = str(get(r, rk["value"]) or "").strip()
            outp = str(get(r, rk["output"]) or "").strip()
            tag = "" if val.lower() in WILD else val
            if outp and outp.lower() not in WILD:
                tag = f"{tag}（{outp}）" if tag else f"（{outp}）"
            if tag:
                base_label = f"{name}·{tag}"
        for u in range(units):
            label = base_label if units == 1 else f"{base_label}（{u + 1}/{units}）"
            tasks.append({
                "row": i + 2, "name": label, "base_name": base_label,
                "type": ctype, "requirement": get(r, rk["requirement"]),
                "value": get(r, rk["value"]), "yuan": yuan, "value_note": vnote,
                "output": get(r, rk["output"]), "qty": qty, "unit": f"{u + 1}/{units}" if units > 1 else "",
                "requester": get(r, rk["requester"]), "rule": rule, "via": via,
                "days": None if rule is None or rule["days"] is None else round(rule["days"], 4),
                "hours": None if rule is None or rule["days"] is None else round(rule["days"] * args.day_hours, 2),
                "priority": rule["priority"] if rule else 10**6,
                "priority_raw": rule["priority_raw"] if rule else "",
                "groups": rule["groups"] if rule else [],
                "needs_confirm": bool(confirm),
                "matched": (f"规则表第 {rule['row']} 行（{rule['task_name']}｜{via}）" if rule else "未匹配"),
            })

    tasks.sort(key=lambda t: (t["priority"], t["row"], t["name"]))
    sched = Scheduler(members, cal, args.day_hours, today, not args.allow_past_start,
                      max_load_days=getattr(args, "max_load_days", None))
    sticky_map: dict[int, str] = {}
    unassigned = 0
    for t in tasks:
        if t["hours"] is None:
            unassigned += 1
            t["assign"] = None
            continue
        sticky = sticky_map.get(t["rule"]["row"]) if (t["rule"] and t["rule"]["sticky"]) else None
        pick, warn = sched.pick(t["groups"], t["hours"], sticky=sticky)
        if warn:
            warnings.append(f"任务「{t['name']}」：{warn}")
        if pick is None:
            unassigned += 1
            t["assign"] = None
            warnings.append(f"任务「{t['name']}」排不出时间：{warn or '无可用成员'}")
            continue
        t["assign"] = pick
        if t["rule"] and t["rule"]["sticky"]:
            sticky_map[t["rule"]["row"]] = pick["name"]
        if pick["start"] <= today <= pick["end"]:
            t["status"] = "进行中"
        elif pick["end"] < today:
            t["status"] = "已完成"
        else:
            t["status"] = "未开始"
        # AI 具名 + 成本（展示名只影响输出，不回灌调度）
        t["executor"] = pick.get("slot") or pick["name"]
        t["model"] = pick.get("model", "")
        t["executor_label"] = ai_display_name(pick, t["name"], args.ai_naming)
        t["cost"], t["cost_note"] = task_cost(t["rule"], pick, t["hours"], args.day_hours)

    # ---- 主任务（形状 C：数量 N → 1 主 + N 子）
    parents = []
    if args.qty_shape == "parent":
        by_row: dict[int, list] = {}
        for t in tasks:
            by_row.setdefault(t["row"], []).append(t)
        for row, ts in sorted(by_row.items()):
            if ts[0]["qty"] <= 1:
                continue
            starts = [t["assign"]["start"] for t in ts if t.get("assign")]
            ends = [t["assign"]["end"] for t in ts if t.get("assign")]
            people = list(dict.fromkeys(t.get("executor_label") or t["assign"]["name"]
                                        for t in ts if t.get("assign")))
            key = f"row{row}"
            for t in ts:
                t["parent_key"] = key
            status = "进行中" if (starts and min(starts) <= today <= max(ends)) else ("已完成" if ends and max(ends) < today else "未开始")
            parents.append({
                "key": key, "row": row, "name": f"{ts[0]['base_name']}×{ts[0]['qty']}",
                "type": ts[0]["type"], "requirement": ts[0]["requirement"], "value": ts[0]["value"],
                "output": ts[0]["output"], "qty": ts[0]["qty"],
                "days": round(sum(t["days"] or 0 for t in ts), 2),
                "hours": round(sum(t["hours"] or 0 for t in ts), 2),
                "priority_raw": ts[0]["priority_raw"], "matched": ts[0]["matched"],
                "start": min(starts) if starts else None, "end": max(ends) if ends else None,
                "designers": people, "status": status,
                "cost": round(sum(t.get("cost") or 0 for t in ts), 4),
                "cost_note": "子任务成本合计",
                "needs_confirm": any(t["needs_confirm"] for t in ts),
            })

    for t in tasks:
        t.setdefault("executor", "")
        t.setdefault("model", "")
        t.setdefault("executor_label", "未分派")
        t.setdefault("cost", None)
        t.setdefault("cost_note", "")

    # ---- 项目：查重 → 前缀 → 命名
    reused = None
    want_base = norm_key(doc_name)
    for pr in projects:
        base = norm_key(re.sub(r"^[A-Za-z]{1,2}\d{1,4}", "", pr["name"]).strip())
        if base and base == want_base and (not type_key or not pr["type"] or pr["type"] == type_key):
            reused = pr["name"]
            break
    planned, prefix_note, _bump = next_prefix(type_key, title_rules, used_letters, warnings)
    if reused:
        project = {"name": reused, "create": False, "prefix": "", "note": f"已有项目「{reused}」去前缀后与文档名同名 → 复用，不新建"}
    else:
        project = {"name": f"{planned}{doc_name}", "create": True, "prefix": planned, "note": prefix_note}

    if not args.projects:
        warnings.append("未提供飞书「项目」表：无法查重，也无法让编号顺延，可能重复建项目")
    if not args.tasks:
        warnings.append("未提供「任务」表：成员空闲只按可安排时间算，未完成任务不会占位")

    confirm_n = sum(1 for t in tasks if t["needs_confirm"]) + sum(1 for p in parents if p["needs_confirm"])

    # ---- 输出
    starts = [t["assign"]["start"] for t in tasks if t.get("assign")]
    ends = [t["assign"]["end"] for t in tasks if t.get("assign")]
    people = {t["assign"]["name"] for t in tasks if t.get("assign")}
    summary = {"项目名称": project["name"], "开始时间": min(starts).isoformat() if starts else "",
               "截止时间": max(ends).isoformat() if ends else "", "任务数": len(tasks) + len(parents),
               "任务参与人数": len(people)}

    L = [f"# 派单方案预览（{today.isoformat()}）", ""]
    L += [f"- 需求文档：{doc_name}｜项目类型：{args.project_type}｜{project['note']}",
          f"- 排期口径：1 天 = {args.day_hours:g} 小时，{cal_note}；在职成员 {active_n}/{len(members)} 人",
          f"- 数量形状：{args.qty_shape}（parent=1主+N子；flat=平铺；single=单条长任务）",
          f"- 设计组：规则表按「设计组」列的先后顺序找有人的组（{'、'.join(group_order) or '未给出'}）", ""]
    L += ["## 项目总结", "", "| 项目名称 | 开始时间 | 截止时间 | 任务数 | 任务参与人数 |",
          "| --- | --- | --- | --- | --- |",
          f"| {summary['项目名称']} | {summary['开始时间']} | {summary['截止时间']} | {summary['任务数']} | {summary['任务参与人数']} |", ""]
    L += ["## 任务清单", "",
          "| # | 主/子 | 名称 | 类型 | 数量 | 工时(天) | 设计师 | 设计组 | 成本({}) | 开始 | 截止 | 排期档 | 状态 | 匹配依据 |".format(args.currency),
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    n = 0
    parent_by_key = {p["key"]: p for p in parents}
    emitted_parent = set()
    for t in tasks:
        pk = t.get("parent_key")
        if pk and pk in parent_by_key and pk not in emitted_parent:
            emitted_parent.add(pk)
            n += 1
            p = parent_by_key[pk]
            a_start = p["start"].isoformat() if p["start"] else ""
            a_end = p["end"].isoformat() if p["end"] else ""
            L.append("| {} | 主 | {} | {} | {} | {} | {} | - | {} | {} | {} | {} | {} | {} |".format(
                n, p["name"], p["type"], p["qty"], p["days"], "、".join(p["designers"]) or "未分派",
                "" if p.get("cost") is None else f"{p['cost']:g}",
                a_start, a_end, p["priority_raw"] or "-", p["status"], p["matched"]))
        n += 1
        a = t.get("assign") or {}
        flag = " ⚠️需确认" if t["needs_confirm"] else ""
        label = ("└ " + t["name"]) if pk else t["name"]
        L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {}{} |".format(
            n, ("子 " + t["unit"]) if pk else "-", label, t["type"], t["qty"] or "",
            "" if t["days"] is None else t["days"], t.get("executor_label") or "未分派", a.get("group", ""),
            "" if t.get("cost") is None else f"{t['cost']:g}",
            a.get("start", ""), a.get("end", ""), t["priority_raw"] or "-", t.get("status", "-"), t["matched"], flag))
    L.append("")
    if confirm_n:
        L += [f"## ❗ {confirm_n} 条需要人工确认", "", "这些行的名称/类型/价值在规则表里是模糊命中或多重命中，已按最可能的规则行预排；确认后才写回。", ""]
    if unassigned:
        L += [f"## ⚠️ {unassigned} 条任务算不出工时", "", "写回前必须由用户补规则或剔除。", ""]
    if warnings:
        L += ["## 告警与需确认项", ""] + [f"- {x}" for x in dict.fromkeys(warnings)] + [""]
    L += ["## 成员占用与负载（仅在职）", "", "| 成员 | 设计组 | 新任务数 | 新增工时(天) | 空闲起点依据 | 已有未完成任务 |",
          "| --- | --- | --- | --- | --- | --- |"]
    inactive = 0
    for m in sorted(sched.members.values(), key=lambda x: (x["group"], x["name"])):
        if not m["active"]:
            inactive += 1
            continue
        cnt = sum(1 for t in tasks if t.get("assign") and t["assign"]["name"] == m["name"])
        L.append(f"| {m['name']} | {m['group'] or '-'} | {cnt} | {round(m['load'] / args.day_hours, 2)} | {m['free_raw']} | {m['busy_span'] or '无'} |")
    L += [f"", f"（另有 {inactive} 名非在职成员不参与派单）", ""]

    # ---- 成本汇总（人机分开；AI 按单次调用计费，人按人天计费）
    cur = args.currency
    is_ai = lambda t: (t.get("assign") or {}).get("kind") == "ai"
    h_cost = round(sum(t.get("cost") or 0 for t in tasks if not is_ai(t)), 2)
    a_cost = round(sum(t.get("cost") or 0 for t in tasks if is_ai(t)), 2)
    n_ai_task = sum(1 for t in tasks if is_ai(t))
    rates = [m.get("cost_day") for m in members if m.get("kind") != "ai" and m.get("cost_day")]
    avg_rate = round(sum(rates) / len(rates), 2) if rates else None
    ai_hours = sum(t["hours"] or 0 for t in tasks if is_ai(t))
    L += ["## 成本汇总（单位：{}）".format(cur), "",
          "| 项 | 任务数 | 工时(天) | 成本 |", "| --- | --- | --- | --- |",
          f"| 人力 | {sum(1 for t in tasks if not is_ai(t) and t.get('assign'))} | "
          f"{round(sum(t['hours'] or 0 for t in tasks if not is_ai(t)) / args.day_hours, 2)} | {h_cost:g} |",
          f"| AI | {n_ai_task} | {round(ai_hours / args.day_hours, 2)} | {a_cost:g} |",
          f"| **合计** | {sum(1 for t in tasks if t.get('assign'))} | "
          f"{round(sum(t['hours'] or 0 for t in tasks if t.get('assign')) / args.day_hours, 2)} | {h_cost + a_cost:g} |", ""]
    if n_ai_task and avg_rate is not None:
        alt = round(avg_rate * ai_hours / args.day_hours, 2)
        L += [f"- 参考：这批 AI 任务若全改由人做（按平均人天成本 {avg_rate:g} {cur}/天）约 {alt:g} {cur}；"
              f"用 AI 花费 {a_cost:g} {cur}，差额 {round(alt - a_cost, 2):g} {cur}", ""]
    if any(t.get("cost") is None and t.get("assign") for t in tasks):
        L += ["- 有任务未填成本单价，未计入合计（成员表补「日成本」/「单次成本」，或规则表补「成本」列）", ""]
    sub_seen, sub_agents = set(), []
    for m in members:
        key = m.get("label") or m.get("name", "")
        if m.get("billing") == "订阅" and m.get("sub_fee") and key not in sub_seen:
            sub_seen.add(key)
            sub_agents.append(m)
    if sub_agents:
        total_sub = round(sum(m["sub_fee"] for m in sub_agents), 2)
        L += [f"- 订阅制 AI：{len(sub_agents)} 个 Agent 走包月/包年分摊（合计订阅费 {total_sub:g} {cur}/周期），"
              f"单次成本按「订阅费 ÷ 预估次数」折算，已在 AI 成本里体现", ""]
        for m in sub_agents:
            runs = m.get("sub_runs")
            nm = m.get("label") or m.get("name")
            if runs is None:
                L += [f"  · {nm}：未填「本月已跑次数」，无法累计已摊（补该列即可）", ""]
                continue
            unit = m.get("cost_run") or 0
            appor = round(min(m["sub_fee"], runs * unit), 2)
            remain = round(m["sub_fee"] - appor, 2)
            vol = m.get("sub_volume") or 0
            over = "（已超预估次数，本月后续边际成本≈0）" if vol and runs > vol else ""
            L += [f"  · {nm}：本月已跑 {runs:g} 次 / 订阅费已摊 {appor:g} {cur}（剩余 {remain:g} {cur}）{over}", ""]

    def fields_of(t, is_parent=False, designers=None):
        """只给可写字段；Lookup/公式列、「需求方」「交接人」「优先级」默认不写。"""
        out = {"名称": t["name"]}
        for key, val in (("类型", t["type"]), ("设计需求", t["requirement"]), ("价值", t["value"]),
                         ("产出位置", t["output"]), ("状态", t.get("status", ""))):
            if val:
                out[key] = val
        if is_parent:
            out["备注"] = f"共 {t['qty']} 件（子任务见父记录关联）"
        if args.write_priority and t.get("priority_raw"):
            out["优先级"] = t["priority_raw"]
        return out

    rows_out = []
    emitted_parent = set()
    for t in tasks:
        pk = t.get("parent_key")
        if pk and pk in parent_by_key and pk not in emitted_parent:
            emitted_parent.add(pk)
            p = parent_by_key[pk]
            rows_out.append({"主/子": "主", "名称": p["name"], "类型": p["type"], "设计需求": p["requirement"],
                             "价值": p["value"], "产出位置": p["output"], "备注": f"共 {p['qty']} 件",
                             "设计师": "、".join(p["designers"]), "所属项目": project["name"],
                             "数量": p["qty"], "工时": p["days"], "小时": p["hours"], "排期档": p["priority_raw"],
                             "开始时间": str(p["start"] or ""), "截止时间": str(p["end"] or ""),
                             "成本": "" if p.get("cost") is None else p["cost"], "成本依据": "子任务合计",
                             "项目类型": args.project_type, "匹配依据": p["matched"]})
        a = t.get("assign") or {}
        f = fields_of(t)
        rows_out.append({"主/子": ("子 " + t["unit"]) if pk else "-",
                         **f, "设计师": t.get("executor_label") or "", "所属项目": project["name"],
                         "数量": t["qty"] or "", "工时": "" if t["days"] is None else t["days"],
                         "小时": "" if t["hours"] is None else t["hours"], "设计组": a.get("group", ""),
                         "执行者": t.get("executor", ""), "模型": t.get("model", ""),
                         "执行者类型": a.get("kind", ""),
                         "成本": "" if t.get("cost") is None else t["cost"], "成本依据": t.get("cost_note", ""),
                         "排期档": t["priority_raw"],
                         "开始时间": str(a.get("start", "")), "截止时间": str(a.get("end", "")),
                         "项目类型": args.project_type, "匹配依据": t["matched"]})
    with (outdir / "tasks.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        fieldnames = list(dict.fromkeys(k for r in rows_out for k in r.keys()))
        wr = csv.DictWriter(fh, fieldnames=fieldnames, restval="")
        wr.writeheader()
        wr.writerows(rows_out)
    (outdir / "plan.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    # ---- 飞书写回载荷
    payload_tasks = []
    for p in parents:
        payload_tasks.append({
            "is_parent": True, "parent_key": p["key"],
            "fields": {**fields_of(p, is_parent=True),
                       "开始时间": to_ms(p["start"], args.tz_offset) if p["start"] else None,
                       "截止时间": to_ms(p["end"], args.tz_offset) if p["end"] else None,
                       "设计师": [{"record_id": f"<成员表记录ID:{x}>"} for x in p["designers"]],
                       "所属项目": [{"record_id": f"<项目表记录ID:{project['name']}>"}]},
            "resolve_links": {"设计师": p["designers"], "所属项目": project["name"]},
            "debug": {"数量": p["qty"], "工时(天)": p["days"], "排期档": p["priority_raw"], "匹配依据": p["matched"]},
        })
    for t in tasks:
        a = t.get("assign") or {}
        entry = {
            "parent_key": t.get("parent_key"),
            "fields": {**fields_of(t),
                       "开始时间": to_ms(a["start"], args.tz_offset) if a else None,
                       "截止时间": to_ms(a["end"], args.tz_offset) if a else None,
                       "设计师": [{"record_id": f"<成员表记录ID:{a.get('label') or a['name']}>"}] if a else [],
                       "所属项目": [{"record_id": f"<项目表记录ID:{project['name']}>"}]},
            # 槽位名（初稿Agent#2）是调度单位，成员表里查不到 → 用基础名 label 解析 record_id
            "resolve_links": {"设计师": a.get("label") or a.get("name", ""), "所属项目": project["name"]},
            "debug": {"匹配依据": t["matched"], "排期档": t["priority"], "工时(天)": t["days"],
                      "小时": t["hours"], "价值换算": t["value_note"], "数量": t["qty"], "序号": t["unit"],
                      "设计组": a.get("group", ""), "需确认": t["needs_confirm"],
                      "展示名": t.get("executor_label", ""), "调度槽": t.get("executor", ""),
                      "模型": t.get("model", ""), "执行者类型": a.get("kind", ""),
                      "成本": t.get("cost"), "成本依据": t.get("cost_note", "")},
        }
        if t.get("parent_key"):
            entry["fields"]["父记录"] = {"record_id": f"<主任务记录ID:{t['parent_key']}>"}
        payload_tasks.append(entry)
    payload = {
        "app_note": "链接字段只给占位符，写回前必须解析成真实 record_id；Lookup/公式字段不要写；"
                    "先建 is_parent=true 的主任务，拿到 record_id 替换子任务里的 <主任务记录ID:...> 再建子任务",
        "project": ({"fields": {"项目名称": project["name"], "项目类型": args.project_type}} if project["create"] else None),
        "project_meta": project,
        "tasks": payload_tasks,
        "readonly_fields": ["项目类型(任务表 Lookup)", "完成情况(Lookup)", "开始时间/结束时间(项目表 Lookup)",
                            "成员(公式)", "可安排时间(Lookup)"],
    }
    (outdir / "feishu_payload.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["成本"] = {"人力": h_cost, "AI": a_cost, "合计": round(h_cost + a_cost, 2),
                   "单位": args.currency, "AI任务数": n_ai_task,
                   "若AI改由人做(参考)": (round(avg_rate * ai_hours / args.day_hours, 2)
                                   if (n_ai_task and avg_rate is not None) else None)}
    (outdir / "plan.json").write_text(json.dumps({
        "summary": summary, "project": project, "today": today.isoformat(),
        "options": {"day_hours": args.day_hours, "qty_shape": args.qty_shape,
                    "holidays": bool(off or work), "allow_past_start": args.allow_past_start},
        "warnings": list(dict.fromkeys(warnings)), "confirm_count": confirm_n,
        "tasks": rows_out,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    if args.emit_prefix_update and project.get("prefix"):
        with (outdir / "项目标题规则.更新.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            wr = csv.writer(fh)
            wr.writerow(["项目类型", "前缀", "说明"])
            wr.writerow([args.project_type, project["prefix"], "本次已用；回写后下次从这一号继续"])
    if args.dump_normalized:
        for name_, src in (("新任务", req_path), ("新类型工时", Path(args.workhours)),
                           ("新项目标题规则", Path(args.title_rules) if args.title_rules else None)):
            if src and Path(src).exists():
                (outdir / f"{name_}.csv").write_text(Path(src).read_text(encoding="utf-8-sig"), encoding="utf-8-sig")

    print("\n".join(L))
    print(f"已写入：{outdir}/plan.md, tasks.csv, plan.json, feishu_payload.json"
          + (", 项目标题规则.更新.csv" if args.emit_prefix_update else ""))
    if unassigned:
        return 3
    return 4 if confirm_n else 0


if __name__ == "__main__":
    sys.exit(main())
