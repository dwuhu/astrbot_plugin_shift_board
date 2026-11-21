"""排班、工时、工薪的纯计算逻辑。

这个模块刻意不依赖 AstrBot，方便单独运行测试：
    python -m tests.test_planner
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
REST_MODE_CHOICES = (4, 6, 8)

_TIME_RE = re.compile(r"^(\d{1,2})\s*[:：时点]\s*(\d{1,2})?$")
_MONTH_RE = re.compile(r"^(\d{4})\s*[-/.年]\s*(\d{1,2})\s*月?$")


# --------------------------------------------------------------------------- #
# 基础类型
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Profile:
    """一个用户（员工）的排班与薪资设置。"""

    rest_mode: int = 4
    shift_start: str = "09:00"
    shift_end: str = "18:00"
    break_minutes: int = 60
    prefer_weekend: bool = True
    hourly_rate: float = 0.0
    monthly_salary: float = 0.0
    pay_mode: str = "hourly"
    overtime_rate: float = 1.5
    standard_hours: float = 8.0
    absence_deduction: bool = False
    rest_day_as_overtime: bool = True

    def normalized(self) -> "Profile":
        """把非法值收敛到可用范围，避免用户输入把统计算崩。"""
        rest_mode = int(self.rest_mode) if str(self.rest_mode).isdigit() else 4
        if rest_mode not in REST_MODE_CHOICES:
            rest_mode = 4
        break_minutes = max(0, min(int(self.break_minutes or 0), 480))
        hourly = max(0.0, float(self.hourly_rate or 0.0))
        monthly = max(0.0, float(self.monthly_salary or 0.0))
        pay_mode = "monthly" if str(self.pay_mode or "").lower() == "monthly" else "hourly"
        if pay_mode == "monthly" and monthly <= 0:
            pay_mode = "hourly"
        overtime = max(1.0, float(self.overtime_rate or 1.0))
        standard_hours = max(1.0, min(float(self.standard_hours or 8.0), 24.0))
        return Profile(
            rest_mode=rest_mode,
            shift_start=normalize_hhmm(self.shift_start, "09:00"),
            shift_end=normalize_hhmm(self.shift_end, "18:00"),
            break_minutes=break_minutes,
            prefer_weekend=bool(self.prefer_weekend),
            hourly_rate=hourly,
            monthly_salary=monthly,
            pay_mode=pay_mode,
            overtime_rate=overtime,
            standard_hours=standard_hours,
            absence_deduction=bool(self.absence_deduction),
            rest_day_as_overtime=bool(self.rest_day_as_overtime),
        )

    @property
    def daily_minutes(self) -> int:
        return shift_minutes(self.shift_start, self.shift_end, self.break_minutes)


@dataclass(slots=True)
class DaySlot:
    """排班表中的一天。"""

    day: str  # ISO 日期，YYYY-MM-DD
    kind: str  # work / rest / leave
    start: str = ""
    end: str = ""
    planned_minutes: int = 0
    weekday: int = 0

    @property
    def is_work(self) -> bool:
        return self.kind == "work"


@dataclass(slots=True)
class Punch:
    """一条打卡记录（一个工作段）。"""

    day: str
    start: str
    end: str | None
    minutes: int
    note: str = ""
    punch_id: int | None = None

    @property
    def is_open(self) -> bool:
        return self.end is None


@dataclass(slots=True)
class DayStats:
    day: str
    weekday: int
    kind: str
    start: str
    end: str
    planned_minutes: int
    actual_minutes: int
    normal_minutes: int
    overtime_minutes: int
    attended: bool
    absent: bool
    is_today: bool
    is_future: bool
    punches: list[Punch] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.kind == "leave":
            return "leave"
        if self.kind == "prejob":
            return "done" if self.attended else "prejob"
        if self.kind == "rest":
            return "overtime" if self.actual_minutes else "rest"
        if self.attended:
            return "done"
        if self.absent:
            return "absent"
        if self.is_future:
            return "planned"
        return "missed"


# --------------------------------------------------------------------------- #
# 时间与日期工具
# --------------------------------------------------------------------------- #
def parse_hhmm(value: object, default: str = "09:00") -> int:
    """把 '09:00' / '9点' / '930' 解析成距零点的分钟数。"""
    if isinstance(value, (int, float)):
        hour = int(value)
        if 0 <= hour <= 23:
            return hour * 60
        value = default
    text = str(value or "").strip() or default
    match = _TIME_RE.match(text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
    digits = re.sub(r"\D", "", text)
    if len(digits) in (3, 4):
        hour = int(digits[:-2])
        minute = int(digits[-2:])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
    hour_only = re.match(r"^(\d{1,2})$", text)
    if hour_only and 0 <= int(hour_only.group(1)) <= 23:
        return int(hour_only.group(1)) * 60
    if default and text != default:
        return parse_hhmm(default, "09:00") if default != "09:00" else 9 * 60
    return 9 * 60


def normalize_hhmm(value: object, default: str = "09:00") -> str:
    return format_hhmm(parse_hhmm(value, default))


def format_hhmm(minutes: int) -> str:
    minutes = int(minutes) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def shift_minutes(start: str, end: str, break_minutes: int = 60) -> int:
    """一个班次的实际工作分钟数，支持跨天夜班，饭休按规则扣除。"""
    start_min = parse_hhmm(start, "09:00")
    end_min = parse_hhmm(end, "18:00")
    if end_min <= start_min:
        end_min += 24 * 60
    total = end_min - start_min
    # 4 小时以下的短班不扣饭休，避免被扣成 0
    if total >= 240 and break_minutes:
        total -= int(break_minutes)
    return max(0, total)


def human_minutes(minutes: int | float) -> str:
    """把分钟数变成 '8.0h' 这种带一位小数的写法。"""
    return f"{float(minutes or 0) / 60:.1f}h"


def human_hours_long(minutes: int | float) -> str:
    minutes = int(round(float(minutes or 0)))
    hours, mins = divmod(minutes, 60)
    if mins:
        return f"{hours}小时{mins}分"
    return f"{hours}小时"


def parse_month(value: object, fallback: date | None = None) -> str:
    """把各种月份写法统一成 YYYY-MM。"""
    fallback = fallback or date.today()
    text = str(value or "").strip()
    if not text:
        return f"{fallback.year:04d}-{fallback.month:02d}"
    match = _MONTH_RE.match(text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}"
    if re.match(r"^\d{1,2}$", text):
        month = int(text)
        if 1 <= month <= 12:
            return f"{fallback.year:04d}-{month:02d}"
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return text[:7]
    return f"{fallback.year:04d}-{fallback.month:02d}"


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = int(month[:4]), int(month[5:7])
    first = date(year, mon, 1)
    last = date(year, mon, calendar.monthrange(year, mon)[1])
    return first, last


def shift_month(month: str, delta: int) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    index = year * 12 + (mon - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_label(month: str) -> str:
    return f"{int(month[:4])}年{int(month[5:7])}月"


# --------------------------------------------------------------------------- #
# 排班算法
# --------------------------------------------------------------------------- #
def _weekend_rests(year: int, month: int, count: int) -> list[int]:
    """尽量把休息日排到真实的周六/周日。返回 1-based 的日期列表。"""
    days_in_month = calendar.monthrange(year, month)[1]
    sundays = [
        day for day in range(1, days_in_month + 1) if date(year, month, day).weekday() == 6
    ]
    pairs = [
        (day, day + 1)
        for day in range(1, days_in_month)
        if date(year, month, day).weekday() == 5
        and date(year, month, day + 1).weekday() == 6
    ]

    picks: list[int] = []
    if count == 4:
        # 单休：每周日，月内 4 或 5 个周日，居中取 4 个
        if len(sundays) >= 4:
            start = (len(sundays) - 4) // 2
            picks = sundays[start : start + 4]
        else:
            picks = list(sundays)
    elif count >= 6 and count % 2 == 0:
        # 双休/大小周：按完整的周末对取，居中
        need = count // 2
        if len(pairs) >= need:
            start = (len(pairs) - need) // 2
            for pair in pairs[start : start + need]:
                picks.extend(pair)

    used = set(picks)
    if len(used) < count:
        # 周末日不够时用离月中最近的周末补齐
        extras = [
            day
            for day in range(1, days_in_month + 1)
            if date(year, month, day).weekday() >= 5 and day not in used
        ]
        extras.sort(key=lambda day: (abs(day - days_in_month / 2), day))
        for day in extras:
            used.add(day)
            if len(used) >= count:
                break
    return sorted(used)


def rest_days_for_month(
    year: int, month: int, rest_mode: int, prefer_weekend: bool = True
) -> list[int]:
    """返回本月休息日的「几号」列表，长度严格等于 rest_mode。

    周末优先时：月休 4 天 ≈ 每周单休，月休 6 天 ≈ 三个双休周末，月休 8 天 ≈ 双休。
    关闭周末优先后，按整月均匀间隔排休。
    """
    days_in_month = calendar.monthrange(year, month)[1]
    count = max(0, min(int(rest_mode or 0), days_in_month))
    if count == 0:
        return []

    if prefer_weekend:
        weekend_picks = _weekend_rests(year, month, count)
        if len(weekend_picks) >= count:
            return sorted(weekend_picks[:count])

    base: list[int] = []
    for index in range(count):
        # 1-based，用区间中点让首尾留白更均匀
        target = round((index + 0.5) * days_in_month / count)
        base.append(min(max(target, 1), days_in_month))

    picks: list[int] = []
    used: set[int] = set()
    for target in base:
        choice = target
        offset = 0
        while choice in used and offset < days_in_month:
            offset += 1
            choice = ((target - 1 + offset) % days_in_month) + 1
        picks.append(choice)
        used.add(choice)

    while len(picks) < count:
        candidates = [day for day in range(1, days_in_month + 1) if day not in used]
        if not candidates:
            break
        picks.append(candidates[0])
        used.add(picks[-1])

    return sorted(used)


def shift_type_label(profile: Profile) -> str:
    """按起止时间自动判断白班 / 夜班（跨天），不需要人手工指定。"""
    profile = profile.normalized()
    start = parse_hhmm(profile.shift_start, "09:00")
    end = parse_hhmm(profile.shift_end, "18:00")
    return "夜班（跨天）" if end <= start else "白班"


def build_month_schedule(
    year: int,
    month: int,
    profile: Profile,
    rest_override: list[int] | None = None,
) -> list[DaySlot]:
    """生成整月排班：休息日按模式排，其余每天按班次时间上班。

    rest_override 不为空时（用户口头指定的「几号休息」），直接按这份日期表排休，
    不再走月休 N 天的自动算法；表里没提到的日期一律算上班。
    """
    profile = profile.normalized()
    days_in_month = calendar.monthrange(year, month)[1]
    if rest_override:
        rests = {int(day) for day in rest_override if 1 <= int(day) <= days_in_month}
    else:
        rests = set(rest_days_for_month(year, month, profile.rest_mode, profile.prefer_weekend))
    daily_minutes = profile.daily_minutes

    slots: list[DaySlot] = []
    for day in range(1, days_in_month + 1):
        current = date(year, month, day)
        iso = current.isoformat()
        if day in rests:
            slots.append(DaySlot(iso, "rest", "", "", 0, current.weekday()))
        else:
            slots.append(
                DaySlot(
                    iso,
                    "work",
                    profile.shift_start,
                    profile.shift_end,
                    daily_minutes,
                    current.weekday(),
                )
            )
    return slots


def build_month_schedule_for(
    month: str, profile: Profile, rest_override: list[int] | None = None
) -> list[DaySlot]:
    return build_month_schedule(
        int(month[:4]), int(month[5:7]), profile, rest_override=rest_override
    )


def week_range(anchor: date | None = None) -> tuple[date, date]:
    anchor = anchor or date.today()
    start = anchor - timedelta(days=anchor.weekday())
    return start, start + timedelta(days=6)


def iter_days(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


# --------------------------------------------------------------------------- #
# 工时与工资
# --------------------------------------------------------------------------- #
def summarize(
    slots: list[DaySlot],
    punches: list[Punch],
    profile: Profile,
    *,
    today: date | None = None,
    month_complete: bool = False,
    fine_total: float = 0.0,
    deductions: list[dict] | None = None,
) -> dict:
    """把排班和打卡揉成一份可直接展示的统计。

    - 计划工时：排班工作日 × 每日工时
    - 标准工时：每天超过「每日标准工时」的部分算加班，默认 8 小时
    - 正常工时：每天 min(实际, 计划, 标准工时)
    - 加班工时：每天超过标准工时的部分；排休日打卡按配置整段算加班
    - 缺勤工时：已过去的工作日里完全没有打卡的那几天
    - 罚款 / 每月固定扣款：从应发里扣掉，得到最终到手
    """
    profile = profile.normalized()
    today = today or date.today()
    standard_minutes = int(round(profile.standard_hours * 60))
    grouped: dict[str, list[Punch]] = {}
    for punch in punches:
        grouped.setdefault(punch.day, []).append(punch)

    # 第一次出现打卡（含进行中）的日期；这之前的排班视为「还没上班」
    first_punch_day = min((punch.day for punch in punches), default=None)

    day_stats: list[DayStats] = []
    for slot in slots:
        current = date.fromisoformat(slot.day)
        is_today = current == today
        is_future = current > today
        day_punches = grouped.get(slot.day, [])
        actual = sum(max(0, int(p.minutes or 0)) for p in day_punches if not p.is_open)
        # 未上班 / 请假仍然保留原班次信息，方便展示和恢复；只是不计入有效工作日
        effective_kind = slot.kind
        if effective_kind == "work" and first_punch_day and slot.day < first_punch_day:
            effective_kind = "prejob"
        planned = slot.planned_minutes

        if effective_kind == "work":
            base = min(planned, standard_minutes)
            normal = min(actual, base)
            overtime = max(0, actual - base)
        elif effective_kind == "rest":
            normal = 0
            overtime = actual if profile.rest_day_as_overtime else 0
        else:
            normal = 0
            overtime = 0

        attended = actual > 0
        # 今天还没下班时不算缺勤；未来、排休、未上班和请假都不算
        absent = bool(
            effective_kind == "work"
            and not is_today
            and not is_future
            and actual == 0
            and day_punches == []
            and first_punch_day is not None
            and slot.day >= first_punch_day
        )
        if not month_complete and is_today and actual == 0:
            absent = False

        day_stats.append(
            DayStats(
                day=slot.day,
                weekday=slot.weekday,
                kind=effective_kind,
                start=slot.start,
                end=slot.end,
                planned_minutes=planned,
                actual_minutes=actual,
                normal_minutes=normal,
                overtime_minutes=overtime,
                attended=attended,
                absent=absent,
                is_today=is_today,
                is_future=is_future,
                punches=day_punches,
            )
        )

    work_days = sum(1 for s in day_stats if s.kind == "work")
    rest_days = sum(1 for s in day_stats if s.kind == "rest")
    leave_days = sum(1 for s in day_stats if s.kind == "leave")
    prejob_days = sum(1 for s in day_stats if s.kind == "prejob")
    contract_work_days = work_days + leave_days + prejob_days
    attended_days = sum(1 for s in day_stats if s.attended)
    work_attended_days = sum(1 for s in day_stats if s.attended and s.kind == "work")
    absence_days = sum(1 for s in day_stats if s.absent)
    overtime_rest_days = sum(1 for s in day_stats if s.kind == "rest" and s.attended)

    planned_total = sum(s.planned_minutes for s in day_stats if s.kind == "work")
    # 合同口径：包含未上班和请假的原班次，用来把月薪折算成时薪和日薪
    contract_planned_total = sum(
        s.planned_minutes for s in day_stats if s.kind != "rest"
    )
    planned_elapsed = sum(
        s.planned_minutes
        for s in day_stats
        if s.kind == "work" and not s.is_future and not s.is_today
    )
    actual_total = sum(s.actual_minutes for s in day_stats)
    normal_total = sum(s.normal_minutes for s in day_stats)
    overtime_total = sum(s.overtime_minutes for s in day_stats)
    absence_minutes = sum(s.planned_minutes for s in day_stats if s.absent)

    rate = profile.hourly_rate
    overtime_rate = profile.overtime_rate
    monthly_mode = (
        profile.pay_mode == "monthly"
        and profile.monthly_salary > 0
        and contract_planned_total > 0
        and contract_work_days > 0
    )
    if monthly_mode:
        # 月薪制：按全月合同工作日折算时薪；未上班和请假的天数不计薪
        rate = profile.monthly_salary / (contract_planned_total / 60)
    normal_pay = normal_total / 60 * rate
    overtime_pay = overtime_total / 60 * rate * overtime_rate
    absence_deduction = (absence_minutes / 60 * rate) if profile.absence_deduction else 0.0
    if monthly_mode:
        # 应发月薪按实际在职工作日比例折算，未上班 / 请假不白拿工资
        normal_pay = profile.monthly_salary * work_days / contract_work_days
        if profile.absence_deduction:
            absence_deduction = (
                profile.monthly_salary / contract_work_days * absence_days
            )
    fine_amount = max(0.0, float(fine_total or 0.0))
    deduction_items = [
        {
            "id": int(item.get("id") or 0),
            "name": str(item.get("name") or "其他"),
            "amount": round(max(0.0, float(item.get("amount") or 0)), 2),
            "repeat_monthly": bool(item.get("repeat_monthly", True)),
            "month": str(item.get("month") or ""),
        }
        for item in (deductions or [])
    ]
    deduction_total = round(
        sum(item["amount"] for item in deduction_items), 2
    )
    gross_pay = normal_pay + overtime_pay - absence_deduction
    pay_before_deductions = gross_pay - fine_amount
    total_pay = pay_before_deductions - deduction_total

    # 全月预测：按当前在职 / 请假口径算应得，加上加班溢价（罚款和扣款已落定）
    if monthly_mode:
        projected_pay = normal_pay + overtime_total / 60 * rate * (
            overtime_rate - 1
        )
    else:
        projected_pay = planned_total / 60 * rate + overtime_total / 60 * rate * (
            overtime_rate - 1
        )
    if profile.absence_deduction and not month_complete:
        projected_pay -= absence_deduction
    projected_pay -= fine_amount
    projected_pay -= deduction_total

    return {
        "work_days": work_days,
        "rest_days": rest_days,
        "leave_days": leave_days,
        "prejob_days": prejob_days,
        "contract_work_days": contract_work_days,
        "attended_days": attended_days,
        "work_attended_days": work_attended_days,
        "absence_days": absence_days,
        "overtime_rest_days": overtime_rest_days,
        "planned_minutes": planned_total,
        "contract_planned_minutes": contract_planned_total,
        "planned_elapsed_minutes": planned_elapsed,
        "actual_minutes": actual_total,
        "normal_minutes": normal_total,
        "overtime_minutes": overtime_total,
        "absence_minutes": absence_minutes,
        "planned_hours": round(planned_total / 60, 2),
        "actual_hours": round(actual_total / 60, 2),
        "normal_hours": round(normal_total / 60, 2),
        "overtime_hours": round(overtime_total / 60, 2),
        "absence_hours": round(absence_minutes / 60, 2),
        "completion": round(actual_total / planned_total * 100, 1) if planned_total else 0.0,
        "shift_label": shift_type_label(profile),
        "daily_minutes": profile.daily_minutes,
        "hourly_rate": round(rate, 2),
        "monthly_salary": round(profile.monthly_salary, 2),
        "pay_mode": "monthly" if monthly_mode else "hourly",
        "overtime_rate": round(overtime_rate, 2),
        "normal_pay": round(normal_pay, 2),
        "overtime_pay": round(overtime_pay, 2),
        "absence_deduction": round(absence_deduction, 2),
        "fine_total": round(fine_amount, 2),
        "deduction_items": deduction_items,
        "deduction_total": deduction_total,
        "pay_before_deductions": round(pay_before_deductions, 2),
        "gross_pay": round(gross_pay, 2),
        "total_pay": round(total_pay, 2),
        "net_pay": round(total_pay, 2),
        "projected_pay": round(projected_pay, 2),
        "standard_hours": round(profile.standard_hours, 1),
        "standard_minutes": standard_minutes,
        "daily_pay": round(
            profile.monthly_salary / contract_work_days
            if monthly_mode
            else profile.daily_minutes / 60 * rate,
            2,
        ),
        "days": day_stats,
    }

