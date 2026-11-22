"""排班工坊 · 工时与工薪 —— AstrBot 插件主文件。

功能：
1. 月休 4 / 6 / 8 天可切换的整月排班表；
2. 上下班打卡、补录、删除，按人累计工时；
3. 按小时工资核算：正常工时 + 加班倍率 - 缺勤扣款；
4. 每个 QQ 用户独立存库，互不影响；
5. WebUI 排班日历 + 工时看板（pages/shift-board）。

数据目录：data/plugin_data/astrbot_plugin_shift_board/shift_board.db
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from . import render
from .planner import (
    DaySlot,
    Profile,
    build_month_schedule_for,
    format_hhmm,
    human_minutes,
    month_bounds,
    month_label,
    normalize_hhmm,
    parse_hhmm,
    parse_month,
    shift_minutes,
    shift_month,
    shift_type_label,
    stage_label,
    summarize,
    week_range,
)
from .store import ShiftStore, UserRecord

PLUGIN_NAME = "astrbot_plugin_shift_board"
_REST_MODES = (4, 6, 8)

# 文转图清晰度：AstrBot 默认 quality=40 的 JPEG，缩到聊天窗口里字会糊。
# 这里统一拉到 100，考勤图再按 760 视口用 1.8 倍设备像素输出。
_T2I_OPTIONS = {"quality": 100, "type": "png"}
_T2I_ATTENDANCE_OPTIONS = {
    "quality": 100,
    "type": "png",
    "scale": "device",
    "device_scale_factor_level": "ultra",
    "viewport_width": 760,
}
_PREFIX_RE = re.compile(r"^[\s/!#！。，,]+")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _now_hhmm() -> str:
    now = datetime.now()
    return format_hhmm(now.hour * 60 + now.minute)


def _parse_day_token(token: str) -> date | None:
    """解析 11-20 / 2026-11-20 / 今天 / 昨天 / 前天。"""
    token = (token or "").strip()
    today = date.today()
    shortcuts = {
        "今天": today,
        "today": today,
        "昨天": today - timedelta(days=1),
        "yesterday": today - timedelta(days=1),
        "前天": today - timedelta(days=2),
    }
    if token in shortcuts:
        return shortcuts[token]

    cleaned = (
        token.replace("年", "-")
        .replace("月", "-")
        .replace("日", "")
        .replace("号", "")
        .replace("/", "-")
        .replace(".", "-")
    )
    match = re.match(r"^(\d{1,2})-(\d{1,2})$", cleaned)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        try:
            return date(today.year, month, day)
        except ValueError:
            return None
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", cleaned)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def _parse_date_range(text: str) -> tuple[date, date] | None:
    """解析「10月1号到10月21号」「2026-10-01 2026-10-21」这类区间。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    parts = [
        part
        for part in re.split(r"\s*(?:到|至|~|～|—|－)\s*", raw)
        if part.strip()
    ]
    dates = [_parse_day_token(part) for part in parts]
    dates = [item for item in dates if item is not None]
    if len(dates) == 1:
        return dates[0], dates[0]
    if len(dates) >= 2:
        start, end = dates[0], dates[1]
        return (start, end) if start <= end else (end, start)
    # 没写「到」时，尝试从长文本里抽出两个日期
    tokens = re.findall(
        r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}\s*[月./-]\s*\d{1,2}\s*[日号]?",
        raw,
    )
    found = [_parse_day_token(token) for token in tokens]
    found = [item for item in found if item is not None]
    if len(found) >= 2:
        return min(found[0], found[1]), max(found[0], found[1])
    return None


def _looks_like_time(token: str) -> bool:
    return bool(re.match(r"^\d{1,2}\s*[:：]\s*\d{0,2}$", token or ""))


def _split_time_range(cell: str) -> tuple[str, str] | None:
    """把 "09:00-18:00" / "9:00~21:30" / "9点至18点" 拆成上下班两个时间。"""
    text = str(cell or "").strip()
    parts = re.split(r"\s*(?:-|~|～|至|到|—)\s*", text)
    if len(parts) == 2 and all(_csv_time(part) for part in parts):
        return _csv_time(parts[0]), _csv_time(parts[1])
    return None


def _read_csv_rows(text: str) -> list[list[str]]:
    """读 CSV 文本：自动认 , / Tab / ; 分隔，按表头找列，并跳过表头。"""
    text = text.lstrip("\ufeff")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    sample = lines[0]
    delimiter = max((",", "\t", ";"), key=lambda mark: sample.count(mark))
    rows = [
        row
        for row in csv.reader(io.StringIO(text), delimiter=delimiter)
        if any(cell.strip() for cell in row)
    ]
    if not rows:
        return []

    columns: tuple[int, int, int, int | None] | None = None
    head = " ".join(rows[0]).lower()
    if any(word in head for word in ("日期", "date", "上班", "下班", "start", "end")):
        def find(*words: str) -> int | None:
            for index, cell in enumerate(rows[0]):
                lowered = str(cell).strip().lower()
                if any(word in lowered for word in words):
                    return index
            return None

        date_at = find("日期", "date")
        start_at = find("上班", "签到", "start", "上班时间")
        end_at = find("下班", "签退", "end", "下班时间")
        if date_at is not None and start_at is not None and end_at is not None:
            columns = (date_at, start_at, end_at, find("备注", "note", "说明"))
        rows = rows[1:]

    normalized: list[list[str]] = []
    for row in rows:
        if columns is not None:
            date_at, start_at, end_at, note_at = columns
            if max(date_at, start_at, end_at) >= len(row):
                normalized.append(row)
                continue
            note = row[note_at] if note_at is not None and note_at < len(row) else ""
            normalized.append([row[date_at], row[start_at], row[end_at], note])
            continue
        if len(row) >= 3:
            if _csv_day(row[0]) is None and len(row) > 3 and _csv_day(row[1]) is not None:
                row = row[1:]  # 首列是姓名的情况
            normalized.append(row)
            continue
        if len(row) == 2:
            split = _split_time_range(row[1])
            if split:
                normalized.append([row[0], split[0], split[1], ""])
                continue
        normalized.append(row)
    return normalized


def _csv_day(token: object) -> date | None:
    raw = str(token or "").strip()
    if not raw:
        return None
    raw = re.split(r"[ T]", raw)[0]
    if raw.isdigit():
        if len(raw) == 5:  # Excel 的日期序列号
            return date(1899, 12, 30) + timedelta(days=int(raw))
        if len(raw) == 8:  # 20261105
            raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return _parse_day_token(raw)


def _csv_time(token: object) -> str | None:
    """吃下常见的表格时间写法：9:00 / 09:00:00 / 9点 / 9点30 / 0930 / 0.375。"""
    raw = str(token or "").strip()
    if not raw:
        return None
    raw = raw.replace("：", ":").replace("．", ".").replace("　", " ")

    # Excel 有时把时间存成一天的小数
    if re.fullmatch(r"0?\.\d+", raw):
        return format_hhmm(int(round(float(raw) * 24 * 60)) % (24 * 60))

    lower = raw.lower()
    is_pm = any(word in raw for word in ("下午", "晚上", "傍晚", "夜")) or "pm" in lower
    is_am = any(word in raw for word in ("上午", "早上", "凌晨")) or "am" in lower
    cleaned = re.sub(r"[上下早中晚夜傍晚凌晨\s]", "", raw)
    cleaned = re.sub(r"(a\.?m\.?|p\.?m\.?)", "", cleaned, flags=re.I)
    cleaned = re.sub(r"点$", "", cleaned).replace("点", ":")
    if not re.match(r"^\d{1,2}([:.点]\d{1,2}){0,2}$", cleaned) and not re.match(
        r"^\d{3,4}$", cleaned
    ):
        return None
    if re.match(r"^\d{3,4}$", cleaned):
        cleaned = f"{cleaned[:-2]}:{cleaned[-2:]}"
    parts = re.split(r"[:.点]", cleaned)
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 and parts[1] else 0
    if is_pm and hour < 12:
        hour += 12
    if is_am and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return format_hhmm(hour * 60 + minute)


def _backup_months(backup: dict) -> list[str]:
    """从备份里抽出有数据的月份，方便前端导入后直接跳过去。"""
    months: set[str] = set()
    tables = backup.get("tables") if isinstance(backup, dict) else None
    if not isinstance(tables, dict):
        return []
    for row in tables.get("punches") or []:
        day = str((row or {}).get("day") or "")
        if len(day) >= 7:
            months.add(day[:7])
    for row in tables.get("schedules") or []:
        month = str((row or {}).get("month") or "")
        if len(month) == 7:
            months.add(month)
    return sorted(months)


def _positive_int(token: str) -> int | None:
    """把 30 / 30分 / 30分钟 解析成非负整数。"""
    cleaned = re.sub(r"[^\d]", "", str(token or ""))
    if not cleaned:
        return None
    return int(cleaned)


def _clamp_float(value: object, low: float, high: float, default: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if number < low:
        return low
    if number > high:
        return high
    return number


def _message_args(event: AstrMessageEvent) -> list[str]:
    """取出「组名 子指令」之后的剩余参数。"""
    text = (getattr(event, "message_str", "") or "").strip()
    text = _PREFIX_RE.sub("", text)
    parts = text.split()
    return parts[2:] if len(parts) >= 3 else []


def _parse_days(text: object) -> list[int]:
    """把 5,14,18,25 / 5号 14号 / 5-7 这类说法解析成日期列表。"""
    raw = re.sub(r"[年月日号\s]", " ", str(text or ""))
    tokens = re.findall(r"\d{1,2}\s*[-~到]\s*\d{1,2}|\d{1,2}", raw)
    days: list[int] = []
    for token in tokens:
        span = re.match(r"^(\d{1,2})\s*[-~到]\s*(\d{1,2})$", token.strip())
        if span:
            start, end = int(span.group(1)), int(span.group(2))
            days.extend(range(min(start, end), max(start, end) + 1))
        else:
            digits = re.sub(r"\D", "", token)
            if digits:
                days.append(int(digits))
    return sorted({day for day in days if 1 <= day <= 31})


def _number(token: object) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", str(token or ""))
    return float(match.group()) if match else None


def _is_month_token(token: object) -> bool:
    text = str(token or "").strip()
    if text in {"本月", "这个月", "当月"}:
        return True
    if re.fullmatch(r"\d{4}[-/.]\d{1,2}", text):
        return True
    return bool(re.search(r"\d", text) and ("月" in text or "年" in text))


def _salary_month(tokens: list[str], default: str = "") -> str:
    """从参数里找月份；没写就落到本月。"""
    for token in reversed(tokens):
        text = str(token or "").strip()
        if text in {"本月", "这个月", "当月"}:
            return f"{date.today():%Y-%m}"
        if re.fullmatch(r"\d{4}[-/.]\d{1,2}", text):
            return parse_month(text)
        if re.search(r"\d", text) and ("月" in text or "年" in text):
            return parse_month(text)
    return parse_month(default)


def _parse_stage(token: object) -> str | None:
    text = str(token or "").strip().lower()
    if text in {"实习", "实习期", "intern", "internship"}:
        return "intern"
    if text in {"正式", "转正", "regular", "fulltime", "full-time"}:
        return "regular"
    return None


def _day_dict(day) -> dict:
    return {
        "day": day.day,
        "weekday": day.weekday,
        "kind": day.kind,
        "start": day.start,
        "end": day.end,
        "planned_minutes": day.planned_minutes,
        "actual_minutes": day.actual_minutes,
        "live_minutes": day.live_minutes,
        "normal_minutes": day.normal_minutes,
        "overtime_minutes": day.overtime_minutes,
        "status": day.status,
        "attended": day.attended,
        "absent": day.absent,
        "is_today": day.is_today,
        "is_future": day.is_future,
        "punches": [
            {
                "id": punch.punch_id,
                "start": punch.start,
                "end": punch.end,
                "minutes": punch.minutes,
                "note": punch.note,
                "open": punch.is_open,
            }
            for punch in day.punches
        ],
    }


def _profile_with_salary(profile: Profile, record: dict | None) -> Profile:
    """用某个月的工资档案覆盖档案里的工资字段，其他设置不动。"""
    if not record:
        return profile
    mode = "monthly" if str(record.get("pay_mode") or "").lower() == "monthly" else "hourly"
    return replace(
        profile,
        pay_mode=mode,
        monthly_salary=max(0.0, float(record.get("monthly_salary") or 0)),
        hourly_rate=max(0.0, float(record.get("hourly_rate") or 0)),
        employment_stage="intern"
        if str(record.get("stage") or "").lower() == "intern"
        else "regular",
    )


def _summary_dict(summary: dict) -> dict:
    return {key: value for key, value in summary.items() if key != "days"}


def _profile_from_summary(profile: Profile, summary: dict) -> Profile:
    return replace(
        profile,
        pay_mode=summary.get("pay_mode", profile.pay_mode),
        monthly_salary=float(summary.get("monthly_salary", profile.monthly_salary) or 0),
        hourly_rate=float(summary.get("hourly_rate", profile.hourly_rate) or 0),
        employment_stage=summary.get("stage", profile.employment_stage),
    )


def _payday_date(salary_month: str, mode: str, day: int) -> date:
    """工资月 -> 实际发薪日。

    month_end：当月最后一天发当月工资；
    next_month：压一个月，次月 day 号发。
    """
    _, month_last = month_bounds(salary_month)
    if mode != "next_month":
        return month_last
    _, next_last = month_bounds(shift_month(salary_month, 1))
    target_day = max(1, min(31, int(day or 15)))
    return date(next_last.year, next_last.month, min(target_day, next_last.day))


def _payday_rule_label(mode: str, day: int) -> str:
    if mode == "next_month":
        return f"压一月 · 次月{max(1, min(31, int(day or 15)))}号发"
    return "当月月底发"


def _realtime_net_pay(summary: dict) -> float:
    """当前月按已出勤天数实时累计的到手工资。

    月薪制：本月应得 ÷ 本月应出勤天数 × 已出勤天数，上一天算一天；
    这样不会月初就显示整月工资，也不会把整月固定扣款一次扣完。
    时薪制：按实际工时累计，再扣罚款；固定扣款按出勤进度分摊。
    """
    total_pay = max(0.0, float(summary.get("total_pay", 0.0) or 0.0))
    work_days = max(0, int(summary.get("work_days", 0) or 0))
    attended = max(0, int(summary.get("work_attended_days", 0) or 0))
    if str(summary.get("pay_mode") or "") == "monthly" and work_days > 0:
        progress = min(1.0, attended / work_days)
        return round(total_pay * progress, 2)
    earned = max(0.0, float(summary.get("earned_pay", 0.0) or 0.0))
    fine = max(0.0, float(summary.get("fine_total", 0.0) or 0.0))
    deduction = max(0.0, float(summary.get("deduction_total", 0.0) or 0.0))
    contract_days = max(0, int(summary.get("contract_work_days", 0) or 0))
    paid_days = max(0, int(summary.get("paid_days", 0) or 0))
    progress = min(1.0, paid_days / contract_days) if contract_days else 1.0
    return round(max(0.0, earned - fine - deduction * progress), 2)


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #
class ShiftBoardPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = config or {}
        data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.store = ShiftStore(data_dir / "shift_board.db")
        self._register_web_apis()
        self._reminder_task: asyncio.Task | None = None
        self._weather_cache: dict[tuple[str, str, str], tuple[float, str]] = {}
        self._start_reminder_task()

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #
    def _register_web_apis(self) -> None:
        routes = (
            ("state", self.api_state, ["GET"], "排班日历与工时看板数据"),
            ("profile", self.api_profile, ["POST"], "保存员工设置"),
            ("mode", self.api_mode, ["POST"], "切换月休方案并重排"),
            ("regenerate", self.api_regenerate, ["POST"], "重新生成整月排班"),
            ("punch", self.api_punch, ["POST"], "补录打卡"),
            ("punch/delete", self.api_punch_delete, ["POST"], "删除打卡"),
            ("day-mark", self.api_day_mark, ["POST"], "标记未上班/请假或恢复工作日"),
            ("deduction/save", self.api_deduction_save, ["POST"], "新增/修改每月扣款"),
            ("deduction/delete", self.api_deduction_delete, ["POST"], "删除每月扣款"),
            ("background", self.api_background, ["GET"], "读取页面背景设置"),
            ("background/save", self.api_background_save, ["POST"], "保存页面背景设置"),
            ("data/export", self.api_data_export, ["GET"], "导出整库备份 JSON"),
            ("data/import", self.api_data_import, ["POST"], "导入整库备份"),
            ("punches/import", self.api_punches_import, ["POST"], "导入打卡 CSV"),
            ("payroll/save", self.api_payroll_save, ["POST"], "保存发薪规则与提醒"),
            ("payroll/pay", self.api_payroll_pay, ["POST"], "登记某月工资到账"),
            ("payroll/unpay", self.api_payroll_unpay, ["POST"], "撤销到账登记"),
        )
        for path, handler, methods, desc in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{path}", handler, methods, desc
            )

    def _default_profile(self) -> Profile:
        cfg = self.config
        return Profile(
            rest_mode=int(cfg.get("default_rest_mode", 4) or 4),
            shift_start=str(cfg.get("default_shift_start", "09:00") or "09:00"),
            shift_end=str(cfg.get("default_shift_end", "18:00") or "18:00"),
            break_minutes=int(cfg.get("default_break_minutes", 60) or 0),
            prefer_weekend=bool(cfg.get("prefer_weekend", True)),
            hourly_rate=float(cfg.get("default_hourly_rate", 0) or 0),
            overtime_rate=float(cfg.get("default_overtime_rate", 1.5) or 1.5),
            standard_hours=float(cfg.get("default_standard_hours", 8.0) or 8.0),
            absence_deduction=bool(cfg.get("absence_deduction", False)),
            rest_day_as_overtime=bool(cfg.get("rest_day_as_overtime", True)),
        ).normalized()

    def _reminder_defaults(self) -> dict:
        cfg = self.config
        return {
            "enabled": bool(cfg.get("remind_enabled", True)),
            "before_start": int(cfg.get("remind_before_start_minutes", 30) or 0),
            "before_end": int(cfg.get("remind_before_end_minutes", 15) or 0),
        }

    def _weather_defaults(self) -> dict:
        cfg = self.config
        return {
            "city": str(cfg.get("default_weather_city", "") or "").strip()[:40],
            "enabled": bool(cfg.get("weather_reminder_enabled", True)),
        }

    async def _user(self, event: AstrMessageEvent) -> UserRecord:
        uid = str(event.get_sender_id() or "unknown").strip() or "unknown"
        platform = (event.get_platform_name() or "").strip() or "unknown"
        # 数据分区键带上平台前缀：同一个人在不同平台、或不同人数字 ID 撞车时不会串数据
        user_key = f"{platform}:{uid}"
        nickname = event.get_sender_name() or uid
        self._start_reminder_task()
        weather = self._weather_defaults()
        return await self.store.ensure_user(
            user_key,
            uid=uid,
            platform=platform,
            nickname=nickname,
            defaults=self._default_profile(),
            umo=str(getattr(event, "unified_msg_origin", "") or ""),
            remind_defaults=self._reminder_defaults(),
            weather_city=weather["city"],
            weather_enabled=weather["enabled"],
        )

    async def _ensure_month(
        self, user_key: str, month: str, profile: Profile
    ) -> list[DaySlot]:
        if not await self.store.has_schedule(user_key, month):
            rests = await self.store.get_month_rests(user_key, month)
            slots = build_month_schedule_for(month, profile, rest_override=rests)
            await self.store.save_schedule(user_key, month, slots)
            return slots
        slots = await self.store.get_schedule(user_key, month)
        if not slots:
            rests = await self.store.get_month_rests(user_key, month)
            slots = build_month_schedule_for(month, profile, rest_override=rests)
            await self.store.save_schedule(user_key, month, slots)
        return slots

    async def _rebuild_month(
        self, user_key: str, month: str, profile: Profile
    ) -> list[DaySlot]:
        rests = await self.store.get_month_rests(user_key, month)
        keep_leave = await self.store.get_leave_days(user_key, month)
        slots = build_month_schedule_for(month, profile, rest_override=rests)
        if keep_leave:
            for slot in slots:
                if int(slot.day[-2:]) in keep_leave and slot.kind == "work":
                    slot.kind = "leave"
        await self.store.save_schedule(user_key, month, slots)
        return slots

    async def _profile_for_month(
        self, user_key: str, month: str, profile: Profile
    ) -> Profile:
        record = await self.store.get_salary_record(user_key, month)
        return _profile_with_salary(profile, record)

    async def _save_month_salary(
        self,
        user_key: str,
        month: str,
        *,
        monthly_salary: float | None = None,
        hourly_rate: float | None = None,
        pay_mode: str | None = None,
        employment_stage: str | None = None,
        note: str = "",
    ) -> UserRecord | None:
        """只改指定月份的工资；改之前先把其他月份按旧口径存档。

        这样 10 月 3000、11 月 5000 可以同时存在，之后涨薪不会回头改写历史月份。
        """
        user = await self.store.get_user(user_key)
        if user is None:
            return None
        existing = await self.store.get_salary_record(user_key, month)
        base = existing or {
            "monthly_salary": user.profile.monthly_salary,
            "hourly_rate": user.profile.hourly_rate,
            "pay_mode": user.profile.pay_mode,
            "stage": user.profile.employment_stage,
        }
        old_monthly = float(base.get("monthly_salary") or 0)
        old_hourly = float(base.get("hourly_rate") or 0)
        old_mode = str(base.get("pay_mode") or "hourly")
        old_stage = str(base.get("stage") or "regular")

        new_monthly = old_monthly if monthly_salary is None else max(0.0, float(monthly_salary))
        new_hourly = old_hourly if hourly_rate is None else max(0.0, float(hourly_rate))
        new_mode = str(pay_mode or "").lower() if pay_mode else old_mode
        if new_mode not in {"hourly", "monthly"}:
            new_mode = old_mode
        if monthly_salary is not None and pay_mode is None and new_monthly > 0:
            new_mode = "monthly"
        if hourly_rate is not None and pay_mode is None and new_hourly > 0:
            new_mode = "hourly"
        new_stage = "intern" if str(employment_stage or old_stage).lower() == "intern" else "regular"

        active_months = await self.store.list_active_months(user_key)
        for other in active_months:
            if other == month:
                continue
            if await self.store.get_salary_record(user_key, other) is None:
                await self.store.save_salary_record(
                    user_key,
                    other,
                    pay_mode=old_mode,
                    monthly_salary=old_monthly,
                    hourly_rate=old_hourly,
                    stage=old_stage,
                    note="历史月份自动存档",
                )
        await self.store.save_salary_record(
            user_key,
            month,
            pay_mode=new_mode,
            monthly_salary=new_monthly,
            hourly_rate=new_hourly,
            stage=new_stage,
            note=note,
        )

        current = f"{date.today():%Y-%m}"
        if month == current:
            await self.store.update_profile(
                user_key,
                pay_mode=new_mode,
                monthly_salary=new_monthly,
                hourly_rate=new_hourly,
                employment_stage=new_stage,
            )
        return await self.store.get_user(user_key)

    async def _month_stats(
        self, user_key: str, month: str, profile: Profile
    ) -> tuple[list[DaySlot], dict]:
        slots = await self._ensure_month(user_key, month, profile)
        profile = await self._profile_for_month(user_key, month, profile)
        first, last = month_bounds(month)
        punches = await self.store.list_punches(
            user_key, first.isoformat(), last.isoformat()
        )
        current_month = f"{date.today():%Y-%m}"
        summary = summarize(
            slots,
            punches,
            profile,
            today=date.today(),
            now=datetime.now(),
            month_complete=month < current_month,
            fine_total=await self.store.fine_total(user_key, month),
            deductions=await self.store.list_deductions(user_key, month),
        )
        return slots, summary

    async def _week_stats(
        self, user_key: str, profile: Profile
    ) -> tuple[date, date, list[DaySlot], dict]:
        start, end = week_range()
        slots: list[DaySlot] = []
        for month in sorted({f"{start:%Y-%m}", f"{end:%Y-%m}"}):
            slots.extend(await self._ensure_month(user_key, month, profile))
        slots = [
            slot for slot in slots if start <= date.fromisoformat(slot.day) <= end
        ]
        punches = await self.store.list_punches(
            user_key, start.isoformat(), end.isoformat()
        )
        summary = summarize(slots, punches, profile, today=date.today())
        return start, end, slots, summary

    async def _day_stats(
        self, user_key: str, day: date, profile: Profile
    ) -> tuple[list[DaySlot], dict]:
        month = f"{day:%Y-%m}"
        slots = await self._ensure_month(user_key, month, profile)
        profile = await self._profile_for_month(user_key, month, profile)
        slot = next((item for item in slots if item.day == day.isoformat()), None)
        if slot is None:
            generated = build_month_schedule_for(month, profile)
            slot = generated[min(day.day, len(generated)) - 1]
        punches = await self.store.list_punches(
            user_key, day.isoformat(), day.isoformat()
        )
        summary = summarize([slot], punches, profile, today=date.today())
        return [slot], summary

    async def _day_earnings(
        self, user: UserRecord, day: date
    ) -> tuple[float, dict]:
        """算某一天实际入账多少钱（时薪制按工时，月薪制按日薪 + 加班）。"""
        profile = user.profile
        _, summary = await self._month_stats(user.user_key, f"{day:%Y-%m}", profile)
        info = next(
            (item for item in summary["days"] if item.day == day.isoformat()), None
        )
        if info is None:
            return 0.0, summary
        if summary["hourly_rate"] <= 0 and summary["monthly_salary"] <= 0:
            return 0.0, summary
        rate = summary["hourly_rate"]
        overtime = info.overtime_minutes / 60 * rate * profile.overtime_rate
        if summary["pay_mode"] == "monthly" and summary["work_days"] > 0:
            base = (
                summary["daily_pay"]
                if info.kind == "work" and info.actual_minutes > 0
                else 0.0
            )
            return base + overtime, summary
        normal = info.normal_minutes / 60 * rate
        return normal + overtime, summary

    async def _annual_income(self, user_key: str, year: str) -> dict:
        """按实际出勤累计年收入：只算已经发生的打卡，不用全月预测。"""
        user = await self.store.get_user(user_key)
        if user is None:
            return {"year": year, "months": [], "total": 0.0, "net_total": 0.0, "paid_days": 0, "by_stage": {}}
        current = f"{date.today():%Y-%m}"
        active = await self.store.list_active_months(user_key, year)
        months = sorted(m for m in active if m.startswith(f"{year}-") and m <= current)
        if current.startswith(f"{year}-") and current not in months:
            months.append(current)
            months.sort()
        rows: list[dict] = []
        by_stage: dict[str, dict] = {
            "intern": {"label": "实习", "total": 0.0, "net_total": 0.0, "paid_days": 0},
            "regular": {"label": "正式", "total": 0.0, "net_total": 0.0, "paid_days": 0},
        }
        for month in months:
            _, summary = await self._month_stats(user_key, month, user.profile)
            stage = summary.get("stage", "regular")
            row = {
                "month": month,
                "month_label": month_label(month),
                "stage": stage,
                "stage_label": summary.get("stage_label", stage_label(stage)),
                "pay_mode": summary.get("pay_mode", "hourly"),
                "monthly_salary": summary.get("monthly_salary", 0),
                "hourly_rate": summary.get("hourly_rate", 0),
                "daily_pay": summary.get("daily_pay", 0),
                "earned_pay": summary.get("earned_pay", 0),
                "earned_net": summary.get("earned_net", 0),
                "paid_days": summary.get("paid_days", 0),
                "attended_days": summary.get("attended_days", 0),
                "actual_hours": summary.get("actual_hours", 0),
                "total_pay": summary.get("total_pay", 0),
            }
            rows.append(row)
            bucket = by_stage.setdefault(
                stage, {"label": stage_label(stage), "total": 0.0, "net_total": 0.0, "paid_days": 0}
            )
            bucket["total"] += row["earned_pay"]
            bucket["net_total"] += row["earned_net"]
            bucket["paid_days"] += row["paid_days"]
        total = round(sum(row["earned_pay"] for row in rows), 2)
        net_total = round(sum(row["earned_net"] for row in rows), 2)
        paid_days = sum(row["paid_days"] for row in rows)
        for bucket in by_stage.values():
            bucket["total"] = round(bucket["total"], 2)
            bucket["net_total"] = round(bucket["net_total"], 2)
        return {
            "year": year,
            "months": rows,
            "total": total,
            "net_total": net_total,
            "paid_days": paid_days,
            "by_stage": by_stage,
        }

    async def _payroll(
        self, user_key: str, *, today: date | None = None
    ) -> dict:
        """按月工资应付 / 已到账 / 未发，以及下一次发薪日。"""
        today = today or date.today()
        current_month = f"{today:%Y-%m}"
        user = await self.store.get_user(user_key)
        if user is None:
            return {
                "mode": "month_end",
                "mode_label": "当月月底发",
                "payday_day": 15,
                "rule_label": _payday_rule_label("month_end", 15),
                "remind_enabled": True,
                "remind_time": "17:00",
                "entries": [],
                "unpaid_total": 0.0,
                "due_unpaid": 0.0,
                "upcoming_unpaid": 0.0,
                "paid_total": 0.0,
                "next": None,
                "current": None,
                "previous": None,
                "calendar": {},
            }
        mode = "next_month" if user.payday_mode == "next_month" else "month_end"
        payday_day = max(1, min(31, int(user.payday_day or 15)))
        active = await self.store.list_active_months(user_key)
        months = [m for m in active if m <= current_month]
        payouts = {
            str(row.get("salary_month") or ""): row
            for row in await self.store.list_payouts(user_key)
        }
        entries: list[dict] = []
        for month in months:
            _, summary = await self._month_stats(user_key, month, user.profile)
            due = round(max(0.0, float(summary.get("total_pay", 0.0) or 0.0)), 2)
            payout = payouts.get(month)
            paid = round(max(0.0, float((payout or {}).get("amount", 0.0) or 0.0)), 2)
            if due <= 0 and paid <= 0:
                continue
            unpaid = round(max(0.0, due - paid), 2)
            payday = _payday_date(month, mode, payday_day)
            days_left = (payday - today).days
            if unpaid <= 0.009:
                status, status_label = "paid", "已到账"
            elif paid > 0:
                status, status_label = "partial", "部分到账"
            elif payday <= today:
                status, status_label = "due", "已到发薪日"
            else:
                status, status_label = "pending", "待发"
            entries.append(
                {
                    "salary_month": month,
                    "month_label": month_label(month),
                    "stage": summary.get("stage", "regular"),
                    "stage_label": summary.get(
                        "stage_label", stage_label(summary.get("stage", "regular"))
                    ),
                    "pay_mode": summary.get("pay_mode", "hourly"),
                    "due": due,
                    "paid": paid,
                    "unpaid": unpaid,
                    "payday": payday.isoformat(),
                    "payday_label": f"{payday.month}月{payday.day}日",
                    "days_left": days_left,
                    "status": status,
                    "status_label": status_label,
                    "paid_on": (payout or {}).get("paid_on", ""),
                    "note": (payout or {}).get("note", ""),
                }
            )
        entries.sort(key=lambda item: item["salary_month"])
        unpaid_entries = [item for item in entries if item["unpaid"] > 0.009]
        due_entries = [
            item
            for item in unpaid_entries
            if date.fromisoformat(item["payday"]) <= today
        ]
        upcoming_entries = [
            item
            for item in unpaid_entries
            if date.fromisoformat(item["payday"]) > today
        ]
        next_entry = (
            sorted(unpaid_entries, key=lambda item: (item["payday"], item["salary_month"]))[0]
            if unpaid_entries
            else None
        )
        calendar: dict[str, list[dict]] = {}
        for item in entries:
            calendar.setdefault(item["payday"], []).append(
                {
                    "salary_month": item["salary_month"],
                    "month_label": item["month_label"],
                    "due": item["due"],
                    "unpaid": item["unpaid"],
                    "status": item["status"],
                    "status_label": item["status_label"],
                }
            )
        current = next((item for item in entries if item["salary_month"] == current_month), None)
        previous = None
        previous_month = shift_month(current_month, -1)
        previous = next((item for item in entries if item["salary_month"] == previous_month), None)
        return {
            "mode": mode,
            "mode_label": "压一月发" if mode == "next_month" else "当月月底发",
            "payday_day": payday_day,
            "rule_label": _payday_rule_label(mode, payday_day),
            "remind_enabled": bool(user.payday_remind_enabled),
            "remind_time": user.payday_remind_time or "17:00",
            "entries": entries,
            "unpaid_total": round(sum(item["unpaid"] for item in unpaid_entries), 2),
            "due_unpaid": round(sum(item["unpaid"] for item in due_entries), 2),
            "upcoming_unpaid": round(sum(item["unpaid"] for item in upcoming_entries), 2),
            "paid_total": round(sum(item["paid"] for item in entries), 2),
            "next": next_entry,
            "current": current,
            "previous": previous,
            "calendar": calendar,
        }

    async def _attendance_data(self, user: UserRecord) -> dict:
        """今日考勤卡片的数据：今天工时、今日按时薪累计收入、本月/上月工资。"""
        today = date.today()
        month = f"{today:%Y-%m}"
        _, summary = await self._month_stats(user.user_key, month, user.profile)
        payroll = await self._payroll(user.user_key, today=today)
        today_stat = next((day for day in summary["days"] if day.is_today), None)
        open_start = summary.get("today_open_start") or ""
        today_minutes = int(summary.get("today_minutes") or 0)
        if open_start:
            status_label, status_class = "上班中", "live"
            detail = (
                f"{open_start} 上班 · 本段 "
                f"{human_minutes(summary.get('today_open_minutes', 0))}"
            )
        elif today_minutes > 0:
            status_label, status_class = "已下班", "done"
            if today_stat:
                detail = f"{today_stat.start}-{today_stat.end} 已下班"
            else:
                detail = f"今天累计 {human_minutes(today_minutes)}"
        elif today_stat is not None and today_stat.kind == "rest":
            status_label, status_class = "今天休息", "rest"
            detail = "今天排休" + (
                f" · 休班加班 {human_minutes(today_stat.actual_minutes)}"
                if today_stat.actual_minutes
                else ""
            )
        elif today_stat is not None and today_stat.kind == "leave":
            status_label, status_class = "请假 / 未上班", "rest"
            detail = "今天请假，不计薪"
        elif today_stat is not None and today_stat.kind == "work":
            status_label, status_class = "还没上班", "idle"
            detail = f"排班 {today_stat.start}-{today_stat.end}"
        else:
            status_label, status_class = "还没上班", "idle"
            detail = "今天还没有打卡"
        remaining = int(summary.get("today_remaining_minutes") or 0)
        if remaining > 0:
            remaining_label = human_minutes(remaining)
        elif status_class == "live":
            remaining_label = "已过下班点"
        else:
            remaining_label = "—"
        current = payroll.get("current") or {}
        previous = payroll.get("previous") or {}
        next_item = payroll.get("next") or {}
        prev_month = shift_month(month, -1)
        current_paid = round(max(0.0, float(current.get("paid", 0) or 0)), 2)
        month_expected = round(
            max(0.0, float(current.get("due", summary.get("total_pay", 0)) or 0)), 2
        )
        # 本月「实得」按已出勤天数累计，今天正在上班也实时增加；
        # 上月及更早月份已经上满，沿用固定的整月工资，不再逐日重算。
        month_earned = _realtime_net_pay(summary)
        month_unpaid = round(max(0.0, month_earned - current_paid), 2)
        older_unpaid = round(
            sum(
                max(0.0, float(item.get("unpaid", 0) or 0))
                for item in payroll.get("entries", [])
                if item.get("salary_month") != month
            ),
            2,
        )
        unpaid_total = round(older_unpaid + month_unpaid, 2)
        if next_item.get("salary_month") == month:
            next_amount = month_unpaid
        else:
            next_amount = round(max(0.0, float(next_item.get("unpaid", 0) or 0)), 2)
        remaining_minutes = max(
            0,
            int(summary.get("planned_minutes") or 0)
            - int(summary.get("actual_minutes") or 0)
            - int(summary.get("today_open_minutes") or 0),
        )
        # 今天这笔按小时实时累计：上了几小时，就加几小时的时薪。
        hourly_rate = round(max(0.0, float(summary.get("hourly_rate") or 0)), 2)
        today_hours = round(today_minutes / 60, 2)
        today_earned = round(today_hours * hourly_rate, 2)
        prev_paid = round(max(0.0, float(previous.get("paid", 0) or 0)), 2)
        return {
            "nickname": user.nickname,
            "date_label": f"{today.month}月{today.day}日",
            "now_label": f"现在 {summary.get('now_label', _now_hhmm())}",
            "today_status": status_label,
            "status_class": status_class,
            "today_minutes_label": human_minutes(today_minutes) if today_minutes else "0小时",
            "today_detail": detail,
            "today_hourly": f"¥{hourly_rate:,.2f}/小时",
            "today_hours_value": f"{today_hours:g}h",
            "today_earned": f"¥{today_earned:,.2f}",
            "today_pay_formula": (
                f"¥{hourly_rate:,.2f}/小时 × {today_hours:g}h"
            ),
            "today_remaining_label": remaining_label,
            "month_label": month_label(month),
            "month_due": f"¥{month_expected:,.2f}",
            "month_earned": f"¥{month_earned:,.2f}",
            "month_paid": f"¥{current_paid:,.2f}",
            "month_unpaid": f"¥{month_unpaid:,.2f}",
            "month_progress": (
                f"已出勤 {summary.get('work_attended_days', 0)}/{summary.get('work_days', 0)} 天"
                " · 上一天算一天"
            ),
            "prev_label": f"{int(prev_month[5:7])}月",
            "prev_due": (
                f"¥{float(previous.get('due', 0) or 0):,.2f}" if previous else "—"
            ),
            "prev_paid": f"¥{prev_paid:,.2f}" if previous else "—",
            "prev_unpaid": (
                f"¥{float(previous.get('unpaid', 0) or 0):,.2f}" if previous else "—"
            ),
            "unpaid_total": f"¥{unpaid_total:,.2f}",
            "older_unpaid": f"¥{older_unpaid:,.2f}",
            "due_unpaid": f"¥{float(payroll.get('due_unpaid', 0) or 0):,.2f}",
            "upcoming_unpaid": f"¥{float(payroll.get('upcoming_unpaid', 0) or 0):,.2f}",
            "next_pay_label": (
                next_item.get("payday_label", "暂无") if next_item else "暂无"
            ),
            "next_amount": f"¥{next_amount:,.2f}" if next_item else "¥0.00",
            "rule_label": payroll.get("rule_label", "当月月底发"),
            "remaining_work": human_minutes(remaining_minutes),
            "month_stats": (
                f"本月出勤 {summary.get('work_attended_days', summary.get('attended_days', 0))}"
                f"/{summary.get('work_days', 0)} 天 · 实际 "
                f"{human_minutes(summary.get('actual_minutes', 0))}"
                f" · 完成率 {float(summary.get('completion', 0) or 0):.1f}%"
                f" · 还要上 {human_minutes(remaining_minutes)}"
            ),
        }

    async def _slot_for_day(
        self, user_key: str, day: date, profile: Profile
    ) -> DaySlot | None:
        slots = await self._ensure_month(user_key, f"{day:%Y-%m}", profile)
        return next((item for item in slots if item.day == day.isoformat()), None)

    # ------------------------------------------------------------------ #
    # 上下班提醒（后台任务）
    # ------------------------------------------------------------------ #
    _REMIND_GRACE_MINUTES = 5

    def _start_reminder_task(self) -> None:
        if self._reminder_task and not self._reminder_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("排班工坊：当前没有事件循环，提醒任务等首次消息时再启动")
            return
        self._reminder_task = loop.create_task(self._reminder_loop())

    async def _reminder_loop(self) -> None:
        await self.store.init()
        try:
            while True:
                try:
                    await self._check_reminders(datetime.now())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"排班工坊：上下班提醒检查出错：{exc}")
                wait = 60 - datetime.now().second + 1
                await asyncio.sleep(wait)
        except asyncio.CancelledError:
            logger.debug("排班工坊：上下班提醒任务已停止")
            raise

    async def _check_reminders(self, now: datetime) -> None:
        if not bool(self.config.get("remind_enabled", True)):
            return
        users = await self.store.list_users(limit=1000)
        for user in users:
            if not user.umo:
                continue
            if user.remind_enabled and (
                user.remind_before_start > 0 or user.remind_before_end > 0
            ):
                try:
                    await self._check_one_reminder(user, now)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"排班工坊：{user.user_key} 的提醒检查失败：{exc}"
                    )
            if (
                user.remind_enabled
                and user.overtime_alert_enabled
                and bool(self.config.get("overtime_alert_enabled", True))
            ):
                try:
                    await self._check_overtime_alert(user, now)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"排班工坊：{user.user_key} 的加班提醒检查失败：{exc}"
                    )
            if user.rest_reminder_enabled:
                try:
                    await self._check_rest_ahead(user, now)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"排班工坊：{user.user_key} 的休息日提醒检查失败：{exc}"
                    )
            if user.payday_remind_enabled:
                try:
                    await self._check_payday_reminder(user, now)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"排班工坊：{user.user_key} 的发薪日提醒检查失败：{exc}"
                    )

    async def _check_one_reminder(self, user: UserRecord, now: datetime) -> None:
        # 夜班的下班时间会落在次日，所以昨天也要看一眼
        for base in (now.date(), now.date() - timedelta(days=1)):
            slot = await self._slot_for_day(user.user_key, base, user.profile)
            if slot is None or slot.kind != "work":
                continue
            start_min = parse_hhmm(slot.start, user.profile.shift_start)
            end_min = parse_hhmm(slot.end, user.profile.shift_end)
            day_zero = datetime(base.year, base.month, base.day)
            start_dt = day_zero + timedelta(minutes=start_min)
            end_dt = day_zero + timedelta(minutes=end_min)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)

            punches = await self.store.list_punches(
                user.user_key, base.isoformat(), end_dt.date().isoformat()
            )

            if user.remind_before_start > 0:
                target = start_dt - timedelta(minutes=user.remind_before_start)
                if self._in_window(now, target) and not punches:
                    if await self.store.mark_reminded(
                        user.user_key, base.isoformat(), "in"
                    ):
                        weather = await self._weather_line(user, "in", base, slot)
                        await self._send_text(
                            user.umo,
                            render.remind_text(
                                kind="in",
                                nickname=user.nickname,
                                day=base,
                                start=slot.start,
                                end=slot.end,
                                minutes=slot.planned_minutes,
                                before=user.remind_before_start,
                                punch_state="none",
                                template=user.remind_in_text,
                                weather=weather,
                            ),
                        )

            if user.remind_before_end > 0:
                target = end_dt - timedelta(minutes=user.remind_before_end)
                closed = any(punch.end for punch in punches)
                if self._in_window(now, target) and not closed:
                    if await self.store.mark_reminded(
                        user.user_key, base.isoformat(), "out"
                    ):
                        opened = next((item for item in punches if item.is_open), None)
                        weather = await self._weather_line(
                            user, "out", end_dt.date(), slot
                        )
                        await self._send_text(
                            user.umo,
                            render.remind_text(
                                kind="out",
                                nickname=user.nickname,
                                day=end_dt.date(),
                                start=slot.start,
                                end=slot.end,
                                minutes=slot.planned_minutes,
                                before=user.remind_before_end,
                                punch_state="open" if opened else "none",
                                open_at=opened.start if opened else "",
                                template=user.remind_out_text,
                                weather=weather,
                                tomorrow_text=(
                                    await self._rest_ahead_text(
                                        user.user_key, end_dt.date(), user.profile
                                    )
                                    if user.rest_reminder_enabled
                                    else ""
                                ),
                            ),
                        )

    async def _rest_ahead_text(
        self, user_key: str, base: date, profile: Profile
    ) -> str:
        """下班提醒里预告接下来的休息日；双休会一次说两天。"""
        first = base + timedelta(days=1)
        slots: list[DaySlot] = []
        for month in sorted({f"{first:%Y-%m}", f"{(first + timedelta(days=6)):%Y-%m}"}):
            slots.extend(await self._ensure_month(user_key, month, profile))
        by_day = {slot.day: slot for slot in slots}
        rest_days: list[date] = []
        for offset in range(7):
            current = first + timedelta(days=offset)
            slot = by_day.get(current.isoformat())
            if slot is None or slot.kind != "rest":
                break
            rest_days.append(current)
        if not rest_days:
            return ""
        if len(rest_days) == 1:
            target = rest_days[0]
            return f"🌙 明天（{target.month}月{target.day}日）休息，今晚不用惦记早起，安心收工。"
        if len(rest_days) == 2:
            first_day, last_day = rest_days
            return (
                f"🌙 明天、后天（{first_day.month}月{first_day.day}日"
                f"-{last_day.month}月{last_day.day}日）都休息，连休两天，今晚可以好好放松。"
            )
        first_day, last_day = rest_days[0], rest_days[-1]
        return (
            f"🌙 明天起连休 {len(rest_days)} 天"
            f"（{first_day.month}月{first_day.day}日-{last_day.month}月{last_day.day}日），"
            "今晚放心收工。"
        )

    async def _check_rest_ahead(self, user: UserRecord, now: datetime) -> None:
        """只按排班表判断：今天上完班、明天休息时，下班后提醒一次。"""
        for base in (now.date(), now.date() - timedelta(days=1)):
            slot = await self._slot_for_day(user.user_key, base, user.profile)
            if slot is None or slot.kind != "work":
                continue
            tomorrow = base + timedelta(days=1)
            next_slot = await self._slot_for_day(
                user.user_key, tomorrow, user.profile
            )
            if next_slot is None or next_slot.kind != "rest":
                continue
            start_min = parse_hhmm(slot.start, user.profile.shift_start)
            end_min = parse_hhmm(slot.end, user.profile.shift_end)
            day_zero = datetime(base.year, base.month, base.day)
            end_dt = day_zero + timedelta(minutes=end_min)
            if end_min <= start_min:
                end_dt += timedelta(days=1)
            if not self._in_window(now, end_dt):
                continue
            line = await self._rest_ahead_text(user.user_key, base, user.profile)
            if not line:
                continue
            if not await self.store.mark_reminded(
                user.user_key, base.isoformat(), "rest_ahead"
            ):
                continue
            await self._send_text(
                user.umo,
                f"🌇 今天辛苦啦，{user.nickname}\n"
                f"{line}\n明天不用早起上班，今晚好好休息。",
            )
            return

    async def _check_payday_reminder(self, user: UserRecord, now: datetime) -> None:
        """发薪日当天，在用户设置的提醒时间问一次「工资到账没有」。"""
        payroll = await self._payroll(user.user_key, today=now.date())
        today_text = now.date().isoformat()
        entries = [
            item
            for item in payroll.get("entries", [])
            if item.get("payday") == today_text and float(item.get("unpaid") or 0) > 0
        ]
        if not entries:
            return
        target_min = parse_hhmm(user.payday_remind_time, "17:00")
        target = datetime(now.year, now.month, now.day) + timedelta(minutes=target_min)
        if not self._in_window(now, target, grace_minutes=240):
            return
        fresh: list[dict] = []
        for item in entries:
            if await self.store.mark_reminded(
                user.user_key, today_text, f"payday:{item['salary_month']}"
            ):
                fresh.append(item)
        if not fresh:
            return
        total = sum(float(item.get("unpaid") or 0) for item in fresh)
        detail = "\n".join(
            f"• {item['month_label']}工资 未发 ¥{float(item['unpaid']):,.2f}"
            f"（{item['payday_label']}发）"
            for item in fresh
        )
        first_month = fresh[0]["salary_month"]
        await self._send_text(
            user.umo,
            f"💰 发工资啦，{user.nickname}\n"
            f"{detail}\n"
            f"合计未发 ¥{total:,.2f}。到账了吗？\n"
            f"收到后回复「/发薪 到账 {first_month}」，"
            "或在后台点「登记到账」，我就把这笔划掉。",
        )

    async def _check_overtime_alert(self, user: UserRecord, now: datetime) -> None:
        """上着班超过每日标准工时时，推送一次加班提醒（按天只发一次）。"""
        for base in (now.date(), now.date() - timedelta(days=1)):
            slot = await self._slot_for_day(user.user_key, base, user.profile)
            if slot is None or slot.kind != "work":
                continue
            punches = await self.store.list_punches(
                user.user_key, base.isoformat(), base.isoformat()
            )
            opened = next((item for item in reversed(punches) if item.is_open), None)
            if opened is None:
                continue
            closed_minutes = sum(
                max(0, int(item.minutes or 0)) for item in punches if not item.is_open
            )
            live_minutes = shift_minutes(
                opened.start, _now_hhmm(), user.profile.break_minutes
            )
            worked = closed_minutes + max(0, live_minutes)
            threshold = int(round(user.profile.standard_hours * 60))
            if worked < threshold:
                continue
            if not await self.store.mark_reminded(
                user.user_key, base.isoformat(), "overtime"
            ):
                return

            month = f"{base:%Y-%m}"
            _, summary = await self._month_stats(user.user_key, month, user.profile)
            overtime_minutes = max(0, worked - threshold)
            rate = float(summary.get("hourly_rate") or 0.0)
            multiplier = float(user.profile.overtime_rate or 1.5)
            if rate > 0:
                extra = overtime_minutes / 60 * rate * max(0.0, multiplier - 1)
                money_line = (
                    f"已产生加班 {human_minutes(overtime_minutes)}，"
                    f"比平时多约 ¥{extra:,.2f}（加班时薪约 ¥{rate * multiplier:g}/h）。"
                )
            else:
                money_line = (
                    f"已产生加班 {human_minutes(overtime_minutes)}，"
                    "先设置工资才能算加班费。"
                )
            await self._send_text(
                user.umo,
                f"⏰ 加班提醒，{user.nickname}\n"
                f"今天已经工作 {human_minutes(worked)}，超过每日标准 "
                f"{user.profile.standard_hours:g} 小时。\n"
                f"从现在起按 ×{multiplier:g} 计加班工资，{money_line}\n"
                "收工记得发「/打卡 下班」。",
            )
            return

    async def _weather_line(
        self, user: UserRecord, kind: str, day: date, slot: DaySlot
    ) -> str:
        """AI 联网查询天气，返回一行可直接拼进提醒的文案；失败则返回空串。"""
        if not bool(self.config.get("weather_reminder_enabled", True)):
            return ""
        if not user.weather_enabled:
            return ""
        city = str(user.weather_city or "").strip()
        if not city:
            return ""

        cache_key = (user.user_key, f"{day.isoformat()}:{kind}", city)
        ttl = max(5, int(self.config.get("weather_cache_minutes", 30) or 30)) * 60
        cached = self._weather_cache.get(cache_key)
        if cached and time.time() - cached[0] < ttl:
            return cached[1]

        try:
            text = await asyncio.wait_for(
                self._build_weather_line(user, kind, day, slot, city), timeout=45
            )
        except asyncio.TimeoutError:
            logger.warning(f"排班工坊：{user.user_key} 天气查询超时")
            text = ""
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"排班工坊：{user.user_key} 天气查询失败：{exc}")
            text = ""
        self._weather_cache[cache_key] = (time.time(), text)
        return text

    async def _build_weather_line(
        self, user: UserRecord, kind: str, day: date, slot: DaySlot, city: str
    ) -> str:
        results = await self._search_weather(user, city, kind, day)
        if not results:
            return ""

        get_provider = getattr(self.context, "get_using_provider_async", None)
        llm_generate = getattr(self.context, "llm_generate", None)
        if not get_provider or not llm_generate:
            return ""
        provider = await get_provider(user.umo or None)
        if provider is None:
            return ""
        provider_id = provider.meta().id

        date_text = f"{day.month}月{day.day}日"
        when = "今天上班通勤时段" if kind == "in" else "今天傍晚下班和明天上班"
        prompt = (
            f"城市：{city}\n日期：{date_text}\n时段：{when}\n"
            f"班次：{slot.start}-{slot.end}\n\n"
            f"联网搜索结果：\n{results[:3000]}\n\n"
            "请只根据上面的搜索结果，写一句 20~50 字的中文天气提醒。"
            "包含天气现象、气温、是否需要带伞或添衣；不要链接、不要来源、不要 Markdown。"
            "如果结果不足以判断天气，只输出：天气信息暂时没查到。"
        )
        response = await asyncio.wait_for(
            llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=(
                    "你是天气播报员。严格依据联网搜索结果，不补充常识、不编造温度；"
                    "输出一行中文，不要解释。"
                ),
            ),
            timeout=30,
        )
        text = re.sub(r"\s+", " ", str(response.completion_text or "")).strip()
        text = text.strip("`*# \"'“”")
        if not text or "没查到" in text or "无法" in text or "抱歉" in text:
            return ""
        if len(text) > 90:
            text = text[:90].rstrip("，。；,; ") + "。"
        return f"🌦 {city} · {text}"

    async def _search_weather(
        self, user: UserRecord, city: str, kind: str, day: date
    ) -> str:
        get_config = getattr(self.context, "get_config", None)
        if not get_config:
            return ""
        cfg = get_config(umo=user.umo or None)
        settings = cfg.get("provider_settings", {}) if hasattr(cfg, "get") else {}
        if not settings.get("web_search", False):
            return ""
        provider_name = str(settings.get("websearch_provider", "tavily") or "tavily")
        tool_names = {
            "tavily": "web_search_tavily",
            "bocha": "web_search_bocha",
            "brave": "web_search_brave",
            "firecrawl": "web_search_firecrawl",
            "baidu_ai_search": "web_search_baidu",
            "exa": "web_search_exa",
            "anysearch": "web_search_anysearch",
            "serpapi": "web_search_serpapi",
            "deepseek": "web_search_deepseek",
        }
        tool_name = tool_names.get(provider_name, "web_search_tavily")
        tool_mgr = self.context.get_llm_tool_manager()
        tool = tool_mgr.get_builtin_tool(tool_name)
        if tool is None:
            return ""
        runtime = SimpleNamespace(
            context=SimpleNamespace(
                context=self.context,
                event=SimpleNamespace(unified_msg_origin=user.umo or ""),
            )
        )
        when = "今天" if kind == "in" else "今天傍晚 明天"
        query = f"{city} {when} 天气 气温 降雨 {day.month}月{day.day}日"
        result = await asyncio.wait_for(tool.call(runtime, query=query), timeout=20)
        text = result if isinstance(result, str) else str(result or "")
        if not text or text.startswith("Error"):
            return ""
        return text

    def _in_window(
        self, now: datetime, target: datetime, *, grace_minutes: int | None = None
    ) -> bool:
        grace = self._REMIND_GRACE_MINUTES if grace_minutes is None else grace_minutes
        return target <= now < target + timedelta(minutes=grace)

    async def _send_text(self, umo: str, text: str) -> bool:
        if not umo:
            return False
        try:
            return bool(
                await self.context.send_message(umo, MessageChain([Plain(text)]))
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"排班工坊：提醒发送失败（{umo}）：{exc}")
            return False

    # ------------------------------------------------------------------ #
    # /排班
    # ------------------------------------------------------------------ #
    @filter.command_group("排班", alias={"shift", "班表"})
    def shift_group(self):
        """排班表：生成、查看、切换月休方案、设置班次时间"""

    @shift_group.command("帮助", alias={"help", "菜单", "指令"})
    async def shift_help(self, event: AstrMessageEvent):
        """查看全部指令（优先发图片）"""
        try:
            url = await self.html_render(
                render.help_html(),
                {},
                return_url=True,
                options=_T2I_OPTIONS,
                umo=event.unified_msg_origin,
            )
        except Exception as exc:  # noqa: BLE001 - t2i 服务未开时回退文本
            logger.warning("帮助卡片渲染图片失败，回退文本：%s", exc)
            yield event.plain_result(render.help_text())
            return
        yield event.image_result(url)

    @shift_group.command("生成", alias={"重排", "generate"})
    async def shift_generate(self, event: AstrMessageEvent):
        """按当前月休方案重排整月"""
        user = await self._user(event)
        args = _message_args(event)
        month = parse_month(args[0] if args else "")
        slots = await self._rebuild_month(user.user_key, month, user.profile)
        _, summary = await self._month_stats(user.user_key, month, user.profile)
        yield event.plain_result(
            render.month_text(
                month,
                user.nickname,
                _profile_from_summary(user.profile, summary),
                [_day_dict(d) for d in summary["days"]],
                summary,
            )
        )
        logger.info(
            "已为用户 %s 重排 %s 班表，共 %d 天", user.user_key, month, len(slots)
        )

    @shift_group.command("查看", alias={"view", "查看本月"})
    async def shift_view(self, event: AstrMessageEvent):
        """查看整月排班与统计"""
        user = await self._user(event)
        args = _message_args(event)
        month = parse_month(args[0] if args else "")
        _, summary = await self._month_stats(user.user_key, month, user.profile)
        yield event.plain_result(
            render.month_text(
                month,
                user.nickname,
                _profile_from_summary(user.profile, summary),
                [_day_dict(d) for d in summary["days"]],
                summary,
            )
        )

    @shift_group.command("本周", alias={"week"})
    async def shift_week(self, event: AstrMessageEvent):
        """查看本周七天"""
        user = await self._user(event)
        start, end, _, summary = await self._week_stats(user.user_key, user.profile)
        yield event.plain_result(
            render.week_text(
                start, end, user.nickname, user.profile, [_day_dict(d) for d in summary["days"]], summary
            )
        )

    @shift_group.command("模式", alias={"月休", "mode"})
    async def shift_mode(self, event: AstrMessageEvent):
        """切换月休 4 / 6 / 8 天"""
        user = await self._user(event)
        args = _message_args(event)
        if not args:
            yield event.plain_result(
                f"当前月休 {user.profile.rest_mode} 天。\n"
                "切换示例：/排班 模式 4（单休）、/排班 模式 6（大小周）、/排班 模式 8（双休）"
            )
            return
        raw = re.sub(r"\D", "", args[0])
        mode = int(raw) if raw else -1
        if mode not in _REST_MODES:
            yield event.plain_result("只支持月休 4 天、6 天、8 天三种方案，例如：/排班 模式 6")
            return
        updated = await self.store.update_profile(user.user_key, rest_mode=mode)
        if updated is None:
            yield event.plain_result("保存失败，请稍后再试。")
            return
        month = parse_month(args[1] if len(args) > 1 else "")
        await self._rebuild_month(updated.user_key, month, updated.profile)
        _, summary = await self._month_stats(updated.user_key, month, updated.profile)
        yield event.plain_result(
            f"✅ 已切换为月休 {mode} 天，{month_label(month)} 班表已重排。\n\n"
            + render.month_text(
                month,
                updated.nickname,
                _profile_from_summary(updated.profile, summary),
                [_day_dict(d) for d in summary["days"]],
                summary,
            )
        )

    @shift_group.command("休息", alias={"排休", "rest"})
    async def shift_rest(self, event: AstrMessageEvent):
        """指定本月哪几天休息，其余天数全上班"""
        user = await self._user(event)
        month = f"{date.today():%Y-%m}"
        args = _message_args(event)
        if not args or args[0] in {"查看", "状态"}:
            rests = await self.store.get_month_rests(user.user_key, month)
            if rests:
                detail = "、".join(f"{day}日" for day in rests)
                text = f"{month_label(month)}指定休息 {len(rests)} 天：{detail}，其余天数照常上班。"
            else:
                text = (
                    f"{month_label(month)}还没指定休息日，当前按月休 {user.profile.rest_mode} 天自动排。"
                )
            yield event.plain_result(
                text + "\n指定：/排班 休息 5,14,18,25　恢复自动：/排班 休息 自动"
            )
            return

        if args[0] in {"自动", "重置", "清除", "取消"}:
            await self.store.clear_month_rests(user.user_key, month)
            slots = await self._rebuild_month(user.user_key, month, user.profile)
            _, summary = await self._month_stats(user.user_key, month, user.profile)
            yield event.plain_result(
                f"✅ 已取消手工指定，{month_label(month)}恢复按月休 {user.profile.rest_mode} 天自动排，"
                f"休息 {summary['rest_days']} 天。"
            )
            return

        days = _parse_days(" ".join(args))
        if not days:
            yield event.plain_result("没听懂休息日期，示例：/排班 休息 5,14,18,25")
            return
        last_day = month_bounds(month)[1].day
        if any(day > last_day for day in days):
            yield event.plain_result(f"{month_label(month)}只有 {last_day} 天，日期超范围了。")
            return

        await self.store.set_month_rests(user.user_key, month, days)
        rest_mode = len(days) if len(days) in _REST_MODES else user.profile.rest_mode
        updated = await self.store.update_profile(user.user_key, rest_mode=rest_mode)
        profile = updated.profile if updated else user.profile
        await self._rebuild_month(user.user_key, month, profile)
        _, summary = await self._month_stats(user.user_key, month, profile)
        detail = "、".join(f"{day}日" for day in days)
        yield event.plain_result(
            f"✅ {month_label(month)}休息日已定为 {len(days)} 天：{detail}\n"
            f"其余 {summary['work_days']} 天按 {profile.shift_start}-{profile.shift_end}"
            f"（{shift_type_label(profile)}）上班，计划 {human_minutes(summary['planned_minutes'])}。"
        )

    @shift_group.command("请假", alias={"未上班", "不上班", "leave"})
    async def shift_leave(self, event: AstrMessageEvent):
        """把某几天标成未上班 / 请假；例：/排班 请假 10月1号到10月21号"""
        user = await self._user(event)
        args = _message_args(event)
        current_month = f"{date.today():%Y-%m}"
        if not args or args[0] in {"查看", "状态", "列表"}:
            month = parse_month(args[1] if len(args) > 1 else current_month)
            leaves = await self.store.get_leave_days(user.user_key, month)
            if leaves:
                detail = "、".join(f"{day}日" for day in leaves)
                text = (
                    f"{month_label(month)}已标记未上班/请假 {len(leaves)} 天：{detail}。\n"
                    "这些天不计薪，月薪会按实际在职工作日折算。"
                )
            else:
                text = f"{month_label(month)}还没有未上班/请假记录。"
            yield event.plain_result(
                text
                + "\n标记：/排班 请假 10月1号到10月21号"
                + "\n恢复：/排班 请假 恢复 10月1号到10月21号"
            )
            return

        clearing = args[0] in {"恢复", "取消", "清除", "销假", "上班"}
        payload = " ".join(args[1:]) if clearing else " ".join(args)
        span = _parse_date_range(payload)
        if span is None:
            # 也接受「/排班 请假 5,6,7」这种本月日期
            days = _parse_days(payload)
            if not days:
                yield event.plain_result(
                    "没听懂日期，示例：/排班 请假 10月1号到10月21号\n"
                    "恢复：/排班 请假 恢复 10月1号到10月21号"
                )
                return
            month = current_month
            start = end = None
            groups = {month: days}
        else:
            start, end = span
            month = f"{start:%Y-%m}"
            groups = {}
            current = start
            while current <= end:
                groups.setdefault(f"{current:%Y-%m}", []).append(current.day)
                current += timedelta(days=1)

        total = 0
        skipped_rest: list[str] = []
        skipped_punch: list[str] = []
        for target_month, days in groups.items():
            await self._ensure_month(user.user_key, target_month, user.profile)
            if clearing:
                total += await self.store.clear_leave_days(
                    user.user_key, target_month, days
                )
                continue

            for day in sorted(set(days)):
                iso = f"{target_month}-{day:02d}"
                slot = await self._slot_for_day(
                    user.user_key, date.fromisoformat(iso), user.profile
                )
                if slot is None or slot.kind == "rest":
                    skipped_rest.append(f"{int(target_month[5:7])}月{day}日")
                    continue
                punches = await self.store.list_punches(
                    user.user_key, iso, iso
                )
                if punches:
                    skipped_punch.append(f"{int(target_month[5:7])}月{day}日")
                    continue
                total += await self.store.set_leave_days(
                    user.user_key, target_month, [day]
                )

        if clearing and groups:
            for target_month in groups:
                await self._rebuild_month(user.user_key, target_month, user.profile)

        month = f"{start:%Y-%m}" if start else month
        _, summary = await self._month_stats(user.user_key, month, user.profile)
        action = "恢复为工作日" if clearing else "标记为未上班/请假"
        lines = [f"✅ {month_label(month)}已{action} {total} 天。"]
        if skipped_punch:
            lines.append(
                "有打卡记录、未处理：" + "、".join(skipped_punch)
                + "（先删掉当天打卡再标记）"
            )
        if skipped_rest:
            lines.append("本来就是休息日、未处理：" + "、".join(skipped_rest))
        lines.append(
            f"当前在职工作日 {summary['work_days']} 天，请假 {summary['leave_days']} 天，"
            f"未上班 {summary['prejob_days']} 天。\n"
            f"应发正常工资 ¥{summary['normal_pay']:,.2f}，全月预测 ¥{summary['projected_pay']:,.2f}。"
        )
        yield event.plain_result("\n".join(lines))

    @shift_group.command("罚款", alias={"扣款", "fine"})
    async def shift_fine(self, event: AstrMessageEvent):
        """记录本月罚款，直接从实发工资里扣"""
        user = await self._user(event)
        month = f"{date.today():%Y-%m}"
        args = _message_args(event)
        fines = await self.store.list_fines(user.user_key, month)

        if not args or args[0] in {"明细", "列表"}:
            if not fines:
                yield event.plain_result(
                    f"{month_label(month)}还没有罚款记录。\n记账：/排班 罚款 200 迟到"
                )
                return
            lines = [f"{month_label(month)}罚款 {len(fines)} 笔："]
            for item in fines:
                reason = item["reason"] or "未写原因"
                lines.append(f"· {reason} -¥{item['amount']:g}")
            total = sum(item["amount"] for item in fines)
            _, summary = await self._month_stats(user.user_key, month, user.profile)
            lines.append(f"合计 -¥{total:g}，实发 ¥{summary['total_pay']:,.2f}")
            yield event.plain_result("\n".join(lines))
            return

        if args[0] in {"清除", "清空", "重置", "删除"}:
            removed = await self.store.clear_fines(user.user_key, month)
            yield event.plain_result(
                f"✅ 已清空 {month_label(month)}的 {removed} 笔罚款，工资按正常口径重算。"
            )
            return

        amount = _number(args[0])
        if amount is None or amount <= 0:
            yield event.plain_result("罚款金额要写正数，例如：/排班 罚款 200 迟到三次")
            return
        if amount > 100000:
            yield event.plain_result("金额太大，请确认后重新录入。")
            return
        reason = " ".join(args[1:]).strip() or "罚款"
        await self.store.add_fine(user.user_key, month, amount, reason)
        _, summary = await self._month_stats(user.user_key, month, user.profile)
        yield event.plain_result(
            f"✅ 已记罚款 ¥{amount:g}（{reason}），{month_label(month)}累计罚款 "
            f"¥{summary['fine_total']:g}，实发 ¥{summary['total_pay']:,.2f}。"
        )

    @shift_group.command("时间", alias={"班次", "shift-time"})
    async def shift_time(self, event: AstrMessageEvent):
        """设置上下班时间和休息分钟"""
        user = await self._user(event)
        args = _message_args(event)
        if len(args) < 2:
            yield event.plain_result(
                f"当前班次 {user.profile.shift_start}-{user.profile.shift_end}"
                f"，每班扣休 {user.profile.break_minutes} 分钟。\n"
                "改班次：/排班 时间 09:00 18:00 60\n"
                "夜班也支持：/排班 时间 22:00 06:00 30"
            )
            return
        start = normalize_hhmm(args[0], "09:00")
        end = normalize_hhmm(args[1], "18:00")
        break_minutes = 60
        if len(args) > 2:
            digits = re.sub(r"\D", "", args[2])
            if digits:
                break_minutes = max(0, min(480, int(digits)))
        updated = await self.store.update_profile(
            user.user_key,
            shift_start=start,
            shift_end=end,
            break_minutes=break_minutes,
        )
        if updated is None:
            yield event.plain_result("保存失败，请稍后再试。")
            return
        month = f"{date.today():%Y-%m}"
        await self._rebuild_month(updated.user_key, month, updated.profile)
        yield event.plain_result(
            f"✅ 班次已更新：{start}-{end}，扣休 {break_minutes} 分钟，"
            f"每班 {human_minutes(updated.profile.daily_minutes)}。本月班表已重排。"
        )

    @shift_group.command("偏好", alias={"prefer"})
    async def shift_prefer(self, event: AstrMessageEvent):
        """休息日是否贴近周末"""
        user = await self._user(event)
        args = _message_args(event)
        if not args:
            yield event.plain_result(
                "当前为"
                + ("周末优先" if user.profile.prefer_weekend else "均匀排休")
                + "。切换：/排班 偏好 周末 或 /排班 偏好 均匀"
            )
            return
        prefer = args[0] not in {"均匀", "平均", "even"}
        updated = await self.store.update_profile(
            user.user_key, prefer_weekend=prefer
        )
        if updated is None:
            yield event.plain_result("保存失败，请稍后再试。")
            return
        month = f"{date.today():%Y-%m}"
        await self._rebuild_month(updated.user_key, month, updated.profile)
        yield event.plain_result(
            f"✅ 已切换为「{'周末优先' if prefer else '均匀排休'}」，本月班表已重排。"
        )

    @shift_group.command("图", alias={"图片", "image"})
    async def shift_image(self, event: AstrMessageEvent):
        """把月历渲染成图片"""
        user = await self._user(event)
        args = _message_args(event)
        month = parse_month(args[0] if args else "")
        _, summary = await self._month_stats(user.user_key, month, user.profile)
        month_profile = _profile_from_summary(user.profile, summary)
        day_dicts = [_day_dict(d) for d in summary["days"]]
        html = render.month_html(month, user.nickname, month_profile, day_dicts, summary)
        try:
            url = await self.html_render(
                html, {}, return_url=True, options=_T2I_OPTIONS, umo=event.unified_msg_origin
            )
        except Exception as exc:  # noqa: BLE001 - t2i 服务未开时回退文本
            logger.warning("排班月历渲染图片失败，回退文本：%s", exc)
            yield event.plain_result(
                render.month_text(month, user.nickname, month_profile, day_dicts, summary)
            )
            return
        yield event.image_result(url)

    @filter.command("今日考勤", alias={"考勤", "attendance"})
    async def today_attendance(self, event: AstrMessageEvent):
        """单独发一张今日考勤图：今天上了多久、本月/上月应发、未发合计。"""
        user = await self._user(event)
        data = await self._attendance_data(user)
        try:
            url = await self.html_render(
                render.attendance_html(data),
                {},
                return_url=True,
                options=_T2I_ATTENDANCE_OPTIONS,
                umo=event.unified_msg_origin,
            )
        except Exception as exc:  # noqa: BLE001 - t2i 服务未开时回退文本
            logger.warning("今日考勤图片渲染失败，回退文本：%s", exc)
            yield event.plain_result(
                f"📋 今日考勤 · {user.nickname}\n"
                f"今天已上 {data['today_minutes_label']}，距下班 {data['today_remaining_label']}\n"
                f"今天已获得 {data['today_earned']}（{data['today_hourly']} × {data['today_hours_value']}）\n"
                f"{data['month_label']}应发 {data['month_due']}，未发 {data['month_unpaid']}\n"
                f"{data['prev_label']}应发 {data['prev_due']}，未发 {data['prev_unpaid']}\n"
                f"所有未发合计 {data['unpaid_total']}"
            )
            return
        yield event.image_result(url)

    # ------------------------------------------------------------------ #
    # /发薪
    # ------------------------------------------------------------------ #
    @filter.command_group("发薪", alias={"发工资", "payday"})
    def payroll_group(self):
        """发薪规则、到账登记与发薪日提醒"""

    @payroll_group.command("查看", alias={"状态", "列表"})
    async def payroll_view(self, event: AstrMessageEvent):
        """查看发薪规则、下一笔和未发合计"""
        user = await self._user(event)
        payroll = await self._payroll(user.user_key)
        yield event.plain_result(render.payroll_center_text(payroll, user.nickname))

    @payroll_group.command("月底", alias={"当月", "same"})
    async def payroll_month_end(self, event: AstrMessageEvent):
        """切换为当月工资当月月底发"""
        user = await self._user(event)
        updated = await self.store.update_profile(user.user_key, payday_mode="month_end")
        if updated is None:
            yield event.plain_result("保存失败，请稍后再试。")
            return
        yield event.plain_result(
            "✅ 已切换为「当月月底发」：每个月最后一天发当月工资。"
            " 查看：/发薪 查看"
        )

    @payroll_group.command("次月", alias={"压一月", "next"})
    async def payroll_next_month(self, event: AstrMessageEvent):
        """切换为压一个月，次月指定日期发"""
        user = await self._user(event)
        args = _message_args(event)
        day = user.payday_day
        if args:
            digits = re.sub(r"\D", "", args[0])
            if digits:
                day = int(digits)
        if not 1 <= day <= 31:
            yield event.plain_result("发薪日需在 1 ~ 31 号之间。")
            return
        updated = await self.store.update_profile(
            user.user_key, payday_mode="next_month", payday_day=day
        )
        if updated is None:
            yield event.plain_result("保存失败，请稍后再试。")
            return
        yield event.plain_result(
            f"✅ 已切换为「压一月」：上月工资在次月 {day} 号发。"
            f" 例如 10 月工资会在 11 月 {day} 号发。"
        )

    @payroll_group.command("提醒", alias={"remind"})
    async def payroll_remind(self, event: AstrMessageEvent):
        """发薪日提醒开关 / 改提醒时间"""
        user = await self._user(event)
        args = _message_args(event)
        if not args:
            yield event.plain_result(
                f"发薪日提醒：{'开' if user.payday_remind_enabled else '关'}"
                f" · 时间 {user.payday_remind_time}"
                "\n改时间：/发薪 提醒 17:30"
                "\n开关：/发薪 提醒 开 或 /发薪 提醒 关"
            )
            return
        token = args[0].strip()
        if token in {"开", "开启", "on"}:
            await self.store.update_profile(user.user_key, payday_remind_enabled=True)
            yield event.plain_result(
                f"✅ 发薪日提醒已开，发薪日 {user.payday_remind_time} 会问工资到账没有。"
            )
            return
        if token in {"关", "关闭", "off"}:
            await self.store.update_profile(user.user_key, payday_remind_enabled=False)
            yield event.plain_result("✅ 发薪日提醒已关。")
            return
        remind_time = normalize_hhmm(token, user.payday_remind_time)
        await self.store.update_profile(
            user.user_key, payday_remind_enabled=True, payday_remind_time=remind_time
        )
        yield event.plain_result(f"✅ 发薪日提醒时间已改为 {remind_time}。")

    @payroll_group.command("到账", alias={"登记", "paid"})
    async def payroll_paid(self, event: AstrMessageEvent):
        """登记某个月工资已到账"""
        user = await self._user(event)
        args = _message_args(event)
        month = _salary_month(args, f"{date.today():%Y-%m}")
        payroll = await self._payroll(user.user_key)
        entry = next(
            (item for item in payroll.get("entries", []) if item["salary_month"] == month),
            None,
        )
        if entry is None:
            yield event.plain_result(f"{month_label(month)}还没有可登记的应发工资。")
            return
        amount = entry["due"]
        if len(args) > 1:
            parsed = _number(args[1])
            if parsed is not None:
                amount = max(0.0, min(float(entry["due"]), float(parsed)))
        await self.store.save_payout(
            user.user_key,
            month,
            amount=amount,
            paid_on=date.today().isoformat(),
            note="聊天登记到账",
        )
        yield event.plain_result(
            f"✅ 已登记 {month_label(month)}工资到账 ¥{amount:,.2f}。"
            f" 查看：/发薪 查看"
        )

    @payroll_group.command("撤销", alias={"撤回", "unpay"})
    async def payroll_unpay(self, event: AstrMessageEvent):
        """撤销某个月工资的到账登记"""
        user = await self._user(event)
        args = _message_args(event)
        month = _salary_month(args, f"{date.today():%Y-%m}")
        deleted = await self.store.delete_payout(user.user_key, month)
        if not deleted:
            yield event.plain_result(f"{month_label(month)}没有到账登记。")
            return
        yield event.plain_result(f"✅ 已撤销 {month_label(month)}工资的到账登记。")

    # ------------------------------------------------------------------ #
    # /打卡
    # ------------------------------------------------------------------ #
    @filter.command_group("打卡", alias={"clock", "punch"})
    def punch_group(self):
        """上下班打卡：上班、下班、状态、补录、删除"""

    @punch_group.command("上班", alias={"in", "签到"})
    async def punch_in(self, event: AstrMessageEvent):
        """记录上班时间"""
        user = await self._user(event)
        today = date.today()
        await self._slot_for_day(user.user_key, today, user.profile)
        punches = await self.store.list_punches(
            user.user_key, today.isoformat(), today.isoformat()
        )
        opened = next((p for p in punches if p.is_open), None)
        if opened:
            yield event.plain_result(
                f"你 {opened.start} 已经打过上班卡了，还在计时中。\n下班后发「/打卡 下班」即可。"
            )
            return
        punch = await self.store.open_punch(user.user_key, today.isoformat(), _now_hhmm())
        yield event.plain_result(
            render.punch_result_text(
                "in",
                {"start": punch.start, "end": "", "minutes": 0},
                user.nickname,
            )
        )

    @punch_group.command("下班", alias={"out", "签退"})
    async def punch_out(self, event: AstrMessageEvent):
        """记录下班时间并结算本段工时"""
        user = await self._user(event)
        today = date.today()
        window_start = today - timedelta(days=1)
        punches = await self.store.list_punches(
            user.user_key, window_start.isoformat(), today.isoformat()
        )
        opened = next((p for p in reversed(punches) if p.is_open), None)
        if opened is None:
            yield event.plain_result(
                "没找到未结束的上班打卡。\n"
                "如果忘了打卡，可以用「/打卡 补 11-20 09:00 18:00」补录，"
                "或用「/打卡 上班」重新开始。"
            )
            return
        open_day = date.fromisoformat(opened.day)
        slot = await self._slot_for_day(user.user_key, open_day, user.profile)
        deduct = user.profile.break_minutes if (slot and slot.is_work) else 0
        end = _now_hhmm()
        minutes = shift_minutes(opened.start, end, deduct)
        closed = await self.store.close_open_punch(
            user.user_key, opened.day, end, minutes
        )
        if closed is None:
            yield event.plain_result("结算打卡失败，请稍后重试。")
            return
        yield event.plain_result(
            render.punch_result_text(
                "out",
                {"start": closed.start, "end": closed.end, "minutes": closed.minutes},
                user.nickname,
            )
        )
        today_punches = await self.store.list_punches(
            user.user_key, open_day.isoformat(), open_day.isoformat()
        )
        closed_minutes_total = sum(
            max(0, int(punch.minutes or 0)) for punch in today_punches if not punch.is_open
        )
        pay, summary = await self._day_earnings(user, open_day)
        if pay > 0:
            money_line = (
                f"今天入账 ¥{pay:,.2f}（实记 {human_minutes(closed_minutes_total)}"
                f"，含本段 {human_minutes(closed.minutes)}）\n"
                f"本月累计实发 ¥{summary['total_pay']:,.2f}"
                f" · 全月预计 ¥{summary['projected_pay']:,.2f}"
            )
        else:
            money_line = (
                "还没设置工资，今天先记工时："
                "发「/工薪 时薪 18」或「/工薪 月薪 5000」就能算钱。\n"
                f"本月累计 实际 {human_minutes(summary['actual_minutes'])}"
                f" / 计划 {human_minutes(summary['planned_minutes'])}"
            )
        yield event.plain_result(money_line)

    @punch_group.command("状态", alias={"status"})
    async def punch_status(self, event: AstrMessageEvent):
        """查看今天是否在班"""
        user = await self._user(event)
        today = date.today()
        _, summary = await self._day_stats(user.user_key, today, user.profile)
        punches = await self.store.list_punches(
            user.user_key, today.isoformat(), today.isoformat()
        )
        opened = next((p for p in punches if p.is_open), None)
        open_info = None
        closed_minutes_total = sum(
            max(0, int(p.minutes or 0)) for p in punches if not p.is_open
        )
        today_minutes = closed_minutes_total
        if opened:
            open_minutes = shift_minutes(opened.start, _now_hhmm(), 0)
            today_minutes += open_minutes
            open_info = {
                "start": opened.start,
                "elapsed_minutes": open_minutes,
            }
        yield event.plain_result(
            render.status_text(
                [_day_dict(d) for d in summary["days"]],
                user.nickname,
                _profile_from_summary(user.profile, summary),
                open_info,
                now_label=datetime.now().strftime("%H:%M"),
                today_minutes=today_minutes,
                today_remaining_minutes=int(summary.get("today_remaining_minutes", 0) or 0),
                today_end_label=str(summary.get("today_end_label", "") or ""),
            )
        )

    @punch_group.command("补", alias={"补录", "fill"})
    async def punch_fill(self, event: AstrMessageEvent):
        """补录某天的上下班时间：/打卡 补 11-20 09:00 18:00"""
        user = await self._user(event)
        args = _message_args(event)
        if len(args) < 2:
            yield event.plain_result(
                "补录格式：/打卡 补 11-20 09:00 18:00\n"
                "只补今天可以写：/打卡 补 09:00 18:00"
            )
            return

        if len(args) >= 3 and not _looks_like_time(args[0]):
            day = _parse_day_token(args[0])
            start_token, end_token = args[1], args[2]
        elif _looks_like_time(args[0]) and _looks_like_time(args[1]):
            day = date.today()
            start_token, end_token = args[0], args[1]
        else:
            yield event.plain_result("日期看不懂，示例：/打卡 补 11-20 09:00 18:00")
            return

        if day is None:
            yield event.plain_result("日期无效，示例：/打卡 补 11-20 09:00 18:00")
            return
        if day > date.today():
            yield event.plain_result("不能给未来的日期补打卡。")
            return

        start = normalize_hhmm(start_token, "09:00")
        end = normalize_hhmm(end_token, "18:00")
        slot = await self._slot_for_day(user.user_key, day, user.profile)
        deduct = user.profile.break_minutes if (slot and slot.is_work) else 0
        minutes = shift_minutes(start, end, deduct)
        if minutes <= 0:
            yield event.plain_result("这段时长算出来是 0，请检查上下班时间。")
            return
        punch = await self.store.add_punch(
            user.user_key, day.isoformat(), start, end, minutes, "补录"
        )
        yield event.plain_result(
            f"✅ 已补录 {day.month}/{day.day} {punch.start}-{punch.end}"
            f"，本段 {human_minutes(punch.minutes)}。"
        )

    @punch_group.command("删", alias={"删除", "delete"})
    async def punch_delete(self, event: AstrMessageEvent):
        """删除某天的打卡：/打卡 删 11-20"""
        user = await self._user(event)
        args = _message_args(event)
        if not args:
            yield event.plain_result("删除格式：/打卡 删 11-20")
            return
        day = _parse_day_token(args[0])
        if day is None:
            yield event.plain_result("日期无效，示例：/打卡 删 11-20")
            return
        removed = await self.store.delete_day_punches(user.user_key, day.isoformat())
        if not removed:
            yield event.plain_result(f"{day.month}/{day.day} 没有打卡记录。")
            return
        yield event.plain_result(
            f"🗑 已删除 {day.month}/{day.day} 的 {removed} 条打卡，工时统计已同步更新。"
        )

    # ------------------------------------------------------------------ #
    # /工时
    # ------------------------------------------------------------------ #
    @filter.command_group("工时", alias={"hours"})
    def hours_group(self):
        """工时统计：今日、本周、本月、明细"""

    @hours_group.command("今日", alias={"today"})
    async def hours_today(self, event: AstrMessageEvent):
        """今天的工时"""
        user = await self._user(event)
        today = date.today()
        _, summary = await self._day_stats(user.user_key, today, user.profile)
        yield event.plain_result(
            render.hours_text(
                f"今日 {today.month}/{today.day}",
                user.nickname,
                _profile_from_summary(user.profile, summary),
                summary,
            )
        )

    @hours_group.command("本周", alias={"week"})
    async def hours_week(self, event: AstrMessageEvent):
        """本周工时"""
        user = await self._user(event)
        start, end, _, summary = await self._week_stats(user.user_key, user.profile)
        yield event.plain_result(
            render.hours_text(
                f"本周 {start.month}/{start.day}~{end.month}/{end.day}",
                user.nickname,
                _profile_from_summary(user.profile, summary),
                summary,
            )
        )

    @hours_group.command("本月", alias={"month"})
    async def hours_month(self, event: AstrMessageEvent):
        """本月工时"""
        user = await self._user(event)
        args = _message_args(event)
        month = parse_month(args[0] if args else "")
        _, summary = await self._month_stats(user.user_key, month, user.profile)
        yield event.plain_result(
            render.hours_text(
                month_label(month),
                user.nickname,
                _profile_from_summary(user.profile, summary),
                summary,
            )
        )

    @hours_group.command("明细", alias={"detail", "清单"})
    async def hours_detail(self, event: AstrMessageEvent):
        """本月每日工时明细"""
        user = await self._user(event)
        args = _message_args(event)
        month = parse_month(args[0] if args else "")
        _, summary = await self._month_stats(user.user_key, month, user.profile)
        detail = [
            _day_dict(day)
            for day in summary["days"]
            if day.actual_minutes or day.absent or day.overtime_minutes
        ]
        yield event.plain_result(
            render.hours_text(
                f"{month_label(month)} 明细",
                user.nickname,
                _profile_from_summary(user.profile, summary),
                summary,
                detail_days=detail,
            )
        )

    # ------------------------------------------------------------------ #
    # /工薪
    # ------------------------------------------------------------------ #
    @filter.command_group("工薪", alias={"pay", "工资"})
    def pay_group(self):
        """工薪分析：设置时薪、本月工资、全月预测、日薪"""

    @pay_group.command("设置", alias={"set", "时薪"})
    async def pay_set(self, event: AstrMessageEvent):
        """设置时薪与加班倍率：/工薪 设置 22.5 1.5"""
        user = await self._user(event)
        args = _message_args(event)
        if not args:
            yield event.plain_result(
                f"当前时薪 ¥{user.profile.hourly_rate:g}/h，加班倍率 ×{user.profile.overtime_rate:g}。\n"
                "设置示例：/工薪 设置 22.5 1.5"
            )
            return
        try:
            rate = float(args[0].replace("¥", "").replace("元", ""))
        except ValueError:
            yield event.plain_result("时薪要填数字，例如：/工薪 设置 22.5 1.5")
            return
        if rate < 0 or rate > 10000:
            yield event.plain_result("时薪范围请控制在 0 ~ 10000 元/小时。")
            return
        overtime = user.profile.overtime_rate
        month_args = args[2:]
        if len(args) > 1:
            if _is_month_token(args[1]):
                month_args = args[1:]
            else:
                try:
                    overtime = float(args[1].replace("×", "").replace("x", ""))
                except ValueError:
                    overtime = user.profile.overtime_rate
                overtime = max(1.0, min(10.0, overtime))
        month = _salary_month(month_args, f"{date.today():%Y-%m}")
        updated = await self._save_month_salary(
            user.user_key, month, hourly_rate=rate, pay_mode="hourly", note="指令设置时薪"
        )
        if updated is None:
            yield event.plain_result("保存失败，请稍后再试。")
            return
        if overtime != user.profile.overtime_rate:
            updated = await self.store.update_profile(user.user_key, overtime_rate=overtime) or updated
        _, summary = await self._month_stats(updated.user_key, month, updated.profile)
        yield event.plain_result(
            f"✅ {month_label(month)} 时薪已设为 ¥{rate:g}/h，加班 ×{overtime:g}，计薪方式「时薪」。\n"
            f"该月按 {updated.profile.shift_start}-{updated.profile.shift_end} 每班 "
            f"{human_minutes(updated.profile.daily_minutes)}，日薪约 ¥{updated.profile.daily_minutes / 60 * rate:,.2f}。\n"
            f"其他月份工资不受影响。\n\n"
            + render.summary_block(summary)
        )

    @pay_group.command("加班", alias={"overtime", "加班费"})
    async def pay_overtime(self, event: AstrMessageEvent):
        """设置加班倍率与每日标准工时：/工薪 加班 1.5 [8]"""
        user = await self._user(event)
        args = _message_args(event)
        if not args:
            yield event.plain_result(
                f"当前加班 ×{user.profile.overtime_rate:g}，每日标准 "
                f"{user.profile.standard_hours:g}h。\n"
                "设置倍率：/工薪 加班 1.5\n"
                "倍率和标准工时一起设：/工薪 加班 1.5 8"
            )
            return
        multiplier = _number(args[0])
        if multiplier is None or not 1.0 <= multiplier <= 10.0:
            yield event.plain_result("加班倍率填 1 ~ 10，例如 /工薪 加班 1.5。")
            return
        standard = user.profile.standard_hours
        if len(args) > 1:
            value = _number(args[1])
            if value is None or not 1 <= value <= 24:
                yield event.plain_result("每日标准工时填 1 ~ 24 小时，例如 /工薪 加班 1.5 8。")
                return
            standard = value
        updated = await self.store.update_profile(
            user.user_key, overtime_rate=multiplier, standard_hours=standard
        )
        if updated is None:
            yield event.plain_result("保存失败，请稍后再试。")
            return
        month = f"{date.today():%Y-%m}"
        _, summary = await self._month_stats(updated.user_key, month, updated.profile)
        yield event.plain_result(
            f"✅ 加班倍率 ×{multiplier:g}，每天超过 {standard:g} 小时算加班。\n"
            f"当前折算时薪 ¥{summary['hourly_rate']:g}/h，"
            f"加班时薪约 ¥{summary['hourly_rate'] * multiplier:g}/h；"
            f"本月已加班 {human_minutes(summary['overtime_minutes'])}。\n"
            "需要上班时提醒：/提醒 加班 开"
        )

    @pay_group.command("月薪", alias={"monthly", "月工资"})
    async def pay_monthly(self, event: AstrMessageEvent):
        """设置月薪：/工薪 月薪 5000"""
        user = await self._user(event)
        args = _message_args(event)
        if not args:
            yield event.plain_result(
                f"当前计薪方式：{'月薪' if user.profile.pay_mode == 'monthly' else '时薪'}，"
                f"月薪 ¥{user.profile.monthly_salary:,.2f}，时薪 ¥{user.profile.hourly_rate:g}/h。\n"
                "设置示例：/工薪 月薪 5000"
            )
            return
        salary = _number(args[0])
        if salary is None or salary <= 0:
            yield event.plain_result("月薪要填正数，例如：/工薪 月薪 5000")
            return
        if salary > 1000000:
            yield event.plain_result("月薪范围请控制在 0 ~ 1000000 元。")
            return
        month = _salary_month(args[1:], f"{date.today():%Y-%m}")
        updated = await self._save_month_salary(
            user.user_key, month, monthly_salary=salary, pay_mode="monthly", note="指令设置月薪"
        )
        if updated is None:
            yield event.plain_result("保存失败，请稍后再试。")
            return
        _, summary = await self._month_stats(updated.user_key, month, updated.profile)
        yield event.plain_result(
            f"✅ {month_label(month)} 月薪已设为 ¥{salary:,.2f}，计薪方式「月薪」。\n"
            f"按该月合同 {summary['contract_work_days']} 个工作日折算，"
            f"时薪约 ¥{summary['hourly_rate']:g}/h，日薪约 ¥{summary['daily_pay']:,.2f}。\n"
            f"其他月份会保留原来的工资，不会被这次涨薪覆盖。\n\n"
            + render.summary_block(summary)
        )

    @pay_group.command("阶段", alias={"stage", "身份"})
    async def pay_stage(self, event: AstrMessageEvent):
        """按月份标记实习 / 正式：/工薪 阶段 实习 2026-10"""
        user = await self._user(event)
        args = _message_args(event)
        if not args:
            records = await self.store.list_salary_records(user.user_key)
            lines = [f"当前身份：{stage_label(user.profile.employment_stage)}"]
            if records:
                lines.append("按月记录：")
                for item in records[-8:]:
                    lines.append(
                        f"· {item['month']} {stage_label(item['stage'])}"
                        f" · 月薪 ¥{float(item['monthly_salary'] or 0):,.2f}"
                        f" / 时薪 ¥{float(item['hourly_rate'] or 0):g}"
                    )
            lines.append("设置示例：/工薪 阶段 实习 2026-10")
            yield event.plain_result("\n".join(lines))
            return
        target = _parse_stage(args[0])
        if target is None:
            yield event.plain_result("身份只支持「实习」或「正式」，例如：/工薪 阶段 正式 2026-11")
            return
        month = _salary_month(args[1:], f"{date.today():%Y-%m}")
        updated = await self._save_month_salary(
            user.user_key, month, employment_stage=target, note="指令设置工资阶段"
        )
        if updated is None:
            yield event.plain_result("保存失败，请稍后再试。")
            return
        _, summary = await self._month_stats(updated.user_key, month, updated.profile)
        pay_text = (
            f"月薪 ¥{summary['monthly_salary']:,.2f}"
            if summary.get("pay_mode") == "monthly"
            else f"时薪 ¥{summary['hourly_rate']:g}/h"
        )
        yield event.plain_result(
            f"✅ {month_label(month)} 已标记为「{stage_label(target)}」，{pay_text}。\n"
            "只改这个月的身份和工资档案，其他月份保持不变。\n"
            "查看全年：/工薪 年收入"
        )

    @pay_group.command("年收入", alias={"年薪", "annual", "year"})
    async def pay_annual(self, event: AstrMessageEvent):
        """按实际打卡累计的年收入，不用未来班表预测。"""
        user = await self._user(event)
        args = _message_args(event)
        year = f"{date.today():%Y}"
        for token in args:
            match = re.search(r"20\d{2}", token)
            if match:
                year = match.group()
                break
        annual = await self._annual_income(user.user_key, year)
        if not annual["months"]:
            yield event.plain_result(f"{year} 年还没有排班或打卡，先发 /排班 生成 排出班表。")
            return
        lines = [
            f"📈 {year} 年实际收入（按已经上班的天数累计，不含未来预测）",
            f"应发 ¥{annual['total']:,.2f} · 到手 ¥{annual['net_total']:,.2f}"
            f" · 已上 {annual['paid_days']} 天",
        ]
        for key in ("intern", "regular"):
            bucket = annual["by_stage"].get(key)
            if bucket and (bucket["paid_days"] or bucket["total"]):
                lines.append(
                    f"· {bucket['label']}期：¥{bucket['total']:,.2f}"
                    f"（到手 ¥{bucket['net_total']:,.2f}，{bucket['paid_days']} 天）"
                )
        lines.append("按月：")
        for row in annual["months"]:
            pay = (
                f"月薪 ¥{row['monthly_salary']:,.2f}"
                if row["pay_mode"] == "monthly"
                else f"时薪 ¥{row['hourly_rate']:g}"
            )
            lines.append(
                f"· {row['month_label']} {row['stage_label']} · {pay}"
                f" · 上 {row['paid_days']} 天 · 应发 ¥{row['earned_pay']:,.2f}"
            )
        yield event.plain_result("\n".join(lines))

    @pay_group.command("本月", alias={"month"})
    async def pay_month(self, event: AstrMessageEvent):
        """本月工资明细"""
        user = await self._user(event)
        args = _message_args(event)
        month = parse_month(args[0] if args else "")
        _, summary = await self._month_stats(user.user_key, month, user.profile)
        yield event.plain_result(
            render.payroll_text(
                month_label(month),
                user.nickname,
                _profile_from_summary(user.profile, summary),
                summary,
                [_day_dict(d) for d in summary["days"]],
            )
        )

    @pay_group.command("预测", alias={"forecast"})
    async def pay_forecast(self, event: AstrMessageEvent):
        """按排班预测全月工资"""
        user = await self._user(event)
        args = _message_args(event)
        month = parse_month(args[0] if args else "")
        _, summary = await self._month_stats(user.user_key, month, user.profile)
        if summary["hourly_rate"] <= 0 and summary["monthly_salary"] <= 0:
            yield event.plain_result(
                "还没设置工资，先发「/工薪 时薪 22.5」或「/工薪 月薪 5000」。"
            )
            return
        planned_hours = summary["planned_minutes"] / 60
        if summary["pay_mode"] == "monthly":
            projected = summary["projected_pay"]
            basis = f"月薪 ¥{summary['monthly_salary']:,.2f}（折算 ¥{summary['hourly_rate']:g}/h）"
        else:
            projected = planned_hours * summary["hourly_rate"]
            projected += (
                summary["overtime_minutes"] / 60
                * summary["hourly_rate"]
                * (user.profile.overtime_rate - 1)
            )
            basis = f"¥{summary['hourly_rate']:g}/h"
        yield event.plain_result(
            f"🔮 {month_label(month)} 工资预测\n"
            f"排班 {summary['work_days']} 天 · 计划 {planned_hours:.1f}h"
            f" · 休 {summary['rest_days']} 天\n"
            f"按 {basis} 干满全月约 ¥{projected:,.2f}\n"
            f"已发生加班 {human_minutes(summary['overtime_minutes'])}"
            f"（×{user.profile.overtime_rate:g} 另计溢价）"
        )

    @pay_group.command("日薪", alias={"daily"})
    async def pay_daily(self, event: AstrMessageEvent):
        """一天多少钱"""
        user = await self._user(event)
        daily_minutes = user.profile.daily_minutes
        rate = user.profile.hourly_rate
        monthly = user.profile.monthly_salary
        if rate <= 0 and monthly <= 0:
            yield event.plain_result("还没设置工资，先发「/工薪 时薪 22.5」或「/工薪 月薪 5000」。")
            return
        month = f"{date.today():%Y-%m}"
        _, summary = await self._month_stats(user.user_key, month, user.profile)
        if summary["pay_mode"] == "monthly":
            daily_text = (
                f"月薪 ¥{monthly:,.2f} ÷ 本月排班 {summary['work_days']} 天"
                f" = 日薪约 ¥{summary['daily_pay']:,.2f}\n"
                f"折算时薪约 ¥{summary['hourly_rate']:g}/h"
            )
        else:
            daily_text = (
                f"日薪 = {human_minutes(daily_minutes)} × ¥{rate:g}"
                f" = ¥{daily_minutes / 60 * rate:,.2f}\n"
                f"月休 {user.profile.rest_mode} 天时，月薪约 "
                f"¥{daily_minutes / 60 * rate * (30 - user.profile.rest_mode):,.2f}"
            )
        yield event.plain_result(
            f"💵 {user.nickname} 日薪核算\n"
            f"班次 {user.profile.shift_start}-{user.profile.shift_end}"
            f" · 扣休 {user.profile.break_minutes} 分"
            f" · 有效 {human_minutes(daily_minutes)}\n"
            + daily_text
        )

    # ------------------------------------------------------------------ #
    # /扣款（每月固定扣款 / 自定义费用标签）
    # ------------------------------------------------------------------ #
    @filter.command_group("扣款", alias={"deduction", "固定扣款", "费用"})
    def deduction_group(self):
        """每月固定扣款：社保、水电、餐费、房租等自定义标签"""

    @deduction_group.command("查看", alias={"列表", "list", "status"})
    async def deduction_list(self, event: AstrMessageEvent):
        """查看某个月生效的扣款明细"""
        user = await self._user(event)
        args = _message_args(event)
        month = parse_month(args[0] if args else "")
        items = await self.store.list_deductions(user.user_key, month)
        _, summary = await self._month_stats(user.user_key, month, user.profile)
        if not items:
            yield event.plain_result(
                f"🧾 {month_label(month)} 还没有固定扣款。\n"
                "添加：/扣款 添加 房租 1500 每月\n"
                "或：/扣款 添加 电费 120 本月"
            )
            return
        lines = [f"🧾 {month_label(month)} 固定扣款："]
        for item in items:
            tag = "每月" if item["repeat_monthly"] else "本月一次"
            lines.append(f"  #{item['id']} {item['name']} ¥{item['amount']:g}（{tag}）")
        lines.append(
            f"合计 -¥{summary['deduction_total']:g}，"
            f"扣款前 ¥{summary['pay_before_deductions']:,.2f} → 到手 ¥{summary['total_pay']:,.2f}"
        )
        yield event.plain_result("\n".join(lines))

    @deduction_group.command("添加", alias={"加", "add", "设置"})
    async def deduction_add(self, event: AstrMessageEvent):
        """添加每月扣款：/扣款 添加 房租 1500 每月"""
        user = await self._user(event)
        args = _message_args(event)
        if len(args) < 2:
            yield event.plain_result(
                "格式：/扣款 添加 标签 金额 [每月|本月]\n"
                "例如：/扣款 添加 房租 1500 每月\n"
                "      /扣款 添加 水电费 120 本月"
            )
            return
        name = args[0].strip()[:20]
        amount = _number(args[1])
        if not name or amount is None or amount <= 0:
            yield event.plain_result("标签不能为空，金额要写正数，例如：/扣款 添加 餐费 600 每月")
            return
        if amount > 1000000:
            yield event.plain_result("扣款金额太大，请确认后再添加。")
            return
        repeat = True
        month = f"{date.today():%Y-%m}"
        for token in args[2:]:
            if token in {"单次", "一次", "本月", "once"}:
                repeat = False
            elif token in {"每月", "固定", "monthly"}:
                repeat = True
            elif re.match(r"^\d{4}-\d{1,2}$|^\d{1,2}$", token):
                month = parse_month(token)
        item = await self.store.add_deduction(
            user.user_key, name, amount, month, repeat_monthly=repeat
        )
        _, summary = await self._month_stats(user.user_key, month, user.profile)
        yield event.plain_result(
            f"✅ 已添加扣款「{item['name']}」¥{amount:g}"
            f"（{'每月' if repeat else '本月一次'}）。\n"
            f"{month_label(month)}固定扣款合计 ¥{summary['deduction_total']:g}，"
            f"到手 ¥{summary['total_pay']:,.2f}。"
        )

    @deduction_group.command("删除", alias={"删", "delete", "移除"})
    async def deduction_delete(self, event: AstrMessageEvent):
        """删除一笔扣款：/扣款 删除 3 或 /扣款 删除 房租"""
        user = await self._user(event)
        args = _message_args(event)
        if not args:
            yield event.plain_result("用法：/扣款 删除 3 或 /扣款 删除 房租")
            return
        token = " ".join(args).strip()
        month = f"{date.today():%Y-%m}"
        if token.isdigit():
            removed = await self.store.delete_deduction(user.user_key, int(token))
            if not removed:
                yield event.plain_result("没有找到这个编号，先发「/扣款 查看」看列表。")
                return
        else:
            item = await self.store.find_deduction_by_name(user.user_key, token, month)
            if item is None:
                yield event.plain_result("没有找到这个标签，先发「/扣款 查看」看列表。")
                return
            await self.store.delete_deduction(user.user_key, item["id"])
        yield event.plain_result(f"🧹 已删除扣款「{token}」。")

    # ------------------------------------------------------------------ #
    # /提醒
    # ------------------------------------------------------------------ #
    @filter.command_group("提醒", alias={"remind", "上下班提醒"})
    def remind_group(self):
        """上下班提醒：开关与提前分钟数"""

    @remind_group.command("状态", alias={"查看", "status"})
    async def remind_status(self, event: AstrMessageEvent):
        """查看当前提醒设置"""
        user = await self._user(event)
        state = "开启" if user.remind_enabled else "关闭"
        channel = "已记录当前会话" if user.umo else "还没记录会话（发一次指令即可）"
        yield event.plain_result(
            f"🔔 {user.nickname} 的上下班提醒：{state}\n"
            f"上班提前 {user.remind_before_start} 分钟 · 下班提前 {user.remind_before_end} 分钟\n"
            f"自定义上班文案：{user.remind_in_text or '未设置（用默认温柔版）'}\n"
            f"自定义下班文案：{user.remind_out_text or '未设置（用默认温柔版）'}\n"
            f"天气城市：{user.weather_city or '未设置'} · 天气提醒：{'开' if user.weather_enabled else '关'}\n"
            f"加班提醒：{'开' if user.overtime_alert_enabled else '关'}"
            f" · 每日标准 {user.profile.standard_hours:g}h"
            f" · 加班 ×{user.profile.overtime_rate:g}\n"
            f"休息日提醒：{'开' if user.rest_reminder_enabled else '关'}"
            "（今天上班、明天排休，下班后提醒一次）\n"
            f"推送会话：{channel}\n"
            "只有排班表里当天是「工作日」时才会提醒。\n"
            "调整：/提醒 开｜/提醒 关｜/提醒 上班 30｜/提醒 下班 15\n"
            "文案：/提醒 自定义 上班 早安 {nickname}，今天 {start}-{end}\n"
            "天气：/提醒 城市 宜宾｜/提醒 天气 开｜/提醒 天气 关\n"
            "加班提醒：/提醒 加班 开｜/提醒 加班 8｜/工薪 加班 1.5\n"
            "休息日提醒：/提醒 休息 开｜/提醒 休息 关"
        )

    @remind_group.command("开", alias={"开启", "on"})
    async def remind_on(self, event: AstrMessageEvent):
        """开启上下班提醒"""
        user = await self._user(event)
        await self.store.update_profile(user.user_key, remind_enabled=True)
        yield event.plain_result(
            f"🔔 已开启，上班前 {user.remind_before_start} 分钟、"
            f"下班前 {user.remind_before_end} 分钟提醒你。"
        )

    @remind_group.command("关", alias={"关闭", "off"})
    async def remind_off(self, event: AstrMessageEvent):
        """关闭上下班提醒"""
        user = await self._user(event)
        await self.store.update_profile(user.user_key, remind_enabled=False)
        yield event.plain_result("🔕 已关闭上下班提醒，随时可以发「/提醒 开」再打开。")

    @remind_group.command("上班", alias={"start"})
    async def remind_before_start(self, event: AstrMessageEvent):
        """设置上班提前多少分钟提醒"""
        user = await self._user(event)
        args = _message_args(event)
        if not args:
            yield event.plain_result(
                f"当前上班提前 {user.remind_before_start} 分钟。用法：/提醒 上班 30"
            )
            return
        minutes = _positive_int(args[0])
        if minutes is None or minutes > 240:
            yield event.plain_result("分钟数要在 0 ~ 240 之间，例如 /提醒 上班 30。")
            return
        await self.store.update_profile(
            user.user_key, remind_before_start=minutes, remind_enabled=True
        )
        yield event.plain_result(f"⏰ 上班提醒已设为提前 {minutes} 分钟。")

    @remind_group.command("下班", alias={"end"})
    async def remind_before_end(self, event: AstrMessageEvent):
        """设置下班提前多少分钟提醒"""
        user = await self._user(event)
        args = _message_args(event)
        if not args:
            yield event.plain_result(
                f"当前下班提前 {user.remind_before_end} 分钟。用法：/提醒 下班 15"
            )
            return
        minutes = _positive_int(args[0])
        if minutes is None or minutes > 240:
            yield event.plain_result("分钟数要在 0 ~ 240 之间，例如 /提醒 下班 15。")
            return
        await self.store.update_profile(
            user.user_key, remind_before_end=minutes, remind_enabled=True
        )
        yield event.plain_result(f"⏰ 下班提醒已设为提前 {minutes} 分钟。")

    @remind_group.command("自定义", alias={"文案", "custom"})
    async def remind_custom(self, event: AstrMessageEvent):
        """自定义上班 / 下班提醒文案"""
        user = await self._user(event)
        raw = _PREFIX_RE.sub("", getattr(event, "message_str", "") or "").strip()
        parts = raw.split(maxsplit=3)
        # parts: [提醒, 自定义, 上班/下班/清空/查看, 文案...]
        target = parts[2] if len(parts) >= 3 else ""
        content = parts[3].strip() if len(parts) >= 4 else ""

        if target in {"查看", "看", "show", ""}:
            yield event.plain_result(
                "📝 当前自定义提醒文案：\n"
                f"上班：{user.remind_in_text or '未设置（默认温柔版）'}\n"
                f"下班：{user.remind_out_text or '未设置（默认温柔版）'}\n"
                "可用占位符：{nickname} {date} {start} {end} {before} {minutes} {type} {greeting} {punch} {weather} {tomorrow}"
            )
            return

        if target in {"清空", "清除", "默认", "reset"}:
            await self.store.update_profile(
                user.user_key, remind_in_text="", remind_out_text=""
            )
            yield event.plain_result("🧹 已清空自定义文案，恢复默认温柔版提醒。")
            return

        if target not in {"上班", "下班", "in", "out"}:
            yield event.plain_result(
                "用法：/提醒 自定义 上班 早安 {nickname}，今天 {start}-{end}\n"
                "或：/提醒 自定义 下班 {nickname} 辛苦啦\n"
                "清空：/提醒 自定义 清空"
            )
            return

        field = "remind_in_text" if target in {"上班", "in"} else "remind_out_text"
        if not content:
            current = user.remind_in_text if field == "remind_in_text" else user.remind_out_text
            yield event.plain_result(
                f"当前{target}文案：{current or '未设置（默认温柔版）'}\n"
                "用法：/提醒 自定义 上班 早安 {nickname}，今天 {start}-{end}\n"
                "可用占位符：{nickname} {date} {start} {end} {before} {minutes} {type} {greeting} {punch} {weather} {tomorrow}"
            )
            return
        if content in {"清空", "清除", "默认", "reset"}:
            content = ""
        if len(content) > 500:
            yield event.plain_result("文案太长了，控制在 500 字以内。")
            return
        await self.store.update_profile(user.user_key, **{field: content})
        yield event.plain_result(
            f"✅ 已保存{target}自定义文案。\n"
            f"下次会按这样发：\n{content or '（已恢复默认）'}\n"
            "提示：把 {weather} 放进文案里，天气会显示在那一行；不写也会自动补在最后。"
        )

    @remind_group.command("城市", alias={"天气城市", "city"})
    async def remind_city(self, event: AstrMessageEvent):
        """设置天气城市（天气由 AI 联网搜索）"""
        user = await self._user(event)
        args = _message_args(event)
        if not args or args[0] in {"查看", "看", "show"}:
            yield event.plain_result(
                f"🌦 当前天气城市：{user.weather_city or '未设置'}\n"
                "设置：/提醒 城市 宜宾\n"
                "需要一个城市名；设置后 AI 会在上下班提醒里联网查天气。"
            )
            return
        city = " ".join(args).strip()[:40]
        if city in {"关", "关闭", "off"}:
            await self.store.update_profile(user.user_key, weather_enabled=False)
            yield event.plain_result("🌦 已关闭天气提醒。")
            return
        await self.store.update_profile(
            user.user_key, weather_city=city, weather_enabled=True
        )
        yield event.plain_result(
            f"✅ 天气城市已设为「{city}」，上下班提醒会让 AI 联网查当天天气。\n"
            "如果天气没出现，检查 AstrBot 的「联网搜索」是否已开启并配好搜索服务。"
        )

    @remind_group.command("天气", alias={"weather"})
    async def remind_weather(self, event: AstrMessageEvent):
        """开关天气提醒"""
        user = await self._user(event)
        args = _message_args(event)
        if not args:
            yield event.plain_result(
                f"🌦 天气提醒：{'开' if user.weather_enabled else '关'} · 城市：{user.weather_city or '未设置'}\n"
                "用法：/提醒 天气 开｜/提醒 天气 关｜/提醒 城市 宜宾"
            )
            return
        action = args[0]
        if action in {"开", "开启", "on"}:
            if not user.weather_city:
                yield event.plain_result("先用「/提醒 城市 宜宾」设置城市，再开天气提醒。")
                return
            await self.store.update_profile(user.user_key, weather_enabled=True)
            yield event.plain_result(
                f"🌦 天气提醒已开启，城市：{user.weather_city}。"
            )
            return
        if action in {"关", "关闭", "off"}:
            await self.store.update_profile(user.user_key, weather_enabled=False)
            yield event.plain_result("🌦 天气提醒已关闭。")
            return
        yield event.plain_result("用法：/提醒 天气 开｜/提醒 天气 关")

    @remind_group.command("加班", alias={"overtime", "加班提醒"})
    async def remind_overtime(self, event: AstrMessageEvent):
        """开关加班提醒，或设置每日标准工时：/提醒 加班 开｜/提醒 加班 8"""
        user = await self._user(event)
        args = _message_args(event)
        if not args or args[0] in {"状态", "查看", "status"}:
            yield event.plain_result(
                f"⏰ 加班提醒：{'开' if user.overtime_alert_enabled else '关'}"
                f" · 每日标准 {user.profile.standard_hours:g}h"
                f" · 加班 ×{user.profile.overtime_rate:g}\n"
                "改标准工时：/提醒 加班 8\n"
                "开关：/提醒 加班 开｜/提醒 加班 关\n"
                "改加班倍率：/工薪 加班 1.5"
            )
            return
        action = args[0]
        if action in {"开", "开启", "on"}:
            await self.store.update_profile(user.user_key, overtime_alert_enabled=True)
            yield event.plain_result(
                f"⏰ 加班提醒已开启：每天超过 {user.profile.standard_hours:g} 小时后提醒一次。"
            )
            return
        if action in {"关", "关闭", "off"}:
            await self.store.update_profile(user.user_key, overtime_alert_enabled=False)
            yield event.plain_result("⏰ 加班提醒已关闭，加班工资仍会照常计算。")
            return
        hours = _number(action)
        if hours is None or not 1 <= hours <= 24:
            yield event.plain_result(
                "标准工时填 1 ~ 24 小时，例如 /提醒 加班 8；开关用 /提醒 加班 开|关。"
            )
            return
        await self.store.update_profile(
            user.user_key, standard_hours=hours, overtime_alert_enabled=True
        )
        yield event.plain_result(
            f"⏰ 已把每日标准工时设为 {hours:g} 小时；超过后按 "
            f"×{user.profile.overtime_rate:g} 计加班，并在上班时提醒一次。"
        )

    @remind_group.command("休息", alias={"休息日", "rest"})
    async def remind_rest(self, event: AstrMessageEvent):
        """休息日提醒：只在前一天上班、后一天排休时，下班后提醒。"""
        user = await self._user(event)
        args = _message_args(event)
        action = args[0] if args else ""
        if not action or action in {"查看", "状态", "status"}:
            state = "开" if user.rest_reminder_enabled else "关"
            yield event.plain_result(
                f"🌙 休息日提醒：{state}\n"
                "只按排班表判断：今天上班、明天排休，才会在下班后提醒一次；"
                "周六周日即使休息，只要排班表写着上班，就不会提醒。\n"
                "开关：/提醒 休息 开｜/提醒 休息 关"
            )
            return
        if action in {"开", "开启", "on"}:
            await self.store.update_profile(user.user_key, rest_reminder_enabled=True)
            yield event.plain_result(
                "🌙 休息日提醒已开启：今天这班结束后、明天排休时，我会提醒你。"
            )
            return
        if action in {"关", "关闭", "off"}:
            await self.store.update_profile(user.user_key, rest_reminder_enabled=False)
            yield event.plain_result("🌙 休息日提醒已关闭。")
            return
        yield event.plain_result("用法：/提醒 休息 开｜/提醒 休息 关")

    # ------------------------------------------------------------------ #
    # LLM 函数工具（让机器人 AI 在聊天里自己调用）
    # ------------------------------------------------------------------ #
    @filter.llm_tool("shift_get_schedule")
    async def ai_schedule(self, event: AstrMessageEvent, month: str = "") -> str:
        """查询当前说话人的整月排班表：工作日、休息日、班次、工时与工资概览。

        这里只返回文字；用户要「排班图 / 班表图片」时请调用 shift_schedule_image。

        Args:
            month(string): 月份，格式 YYYY-MM；留空表示本月。
        """
        user = await self._user(event)
        target = parse_month(month)
        _, summary = await self._month_stats(user.user_key, target, user.profile)
        rest_days = [
            f"{int(day.day[8:10])}日" for day in summary["days"] if day.kind == "rest"
        ]
        rest_text = "、".join(rest_days[:12]) if rest_days else "无"
        if len(rest_days) > 12:
            rest_text += f" 等 {len(rest_days)} 天"
        extra = ""
        if summary["work_days"]:
            first_work = next(
                d for d in summary["days"] if d.kind == "work"
            )
            extra = f"，班次 {first_work.start}-{first_work.end}"
        return (
            f"{user.nickname} · {month_label(target)} 排班\n"
            f"月休方案 {user.profile.rest_mode} 天{extra}（{shift_type_label(user.profile)}，每班 {human_minutes(user.profile.daily_minutes)}）\n"
            f"工作日 {summary['work_days']} 天，休息日 {summary['rest_days']} 天（{rest_text}）\n"
            f"计划 {human_minutes(summary['planned_minutes'])}"
            f"，已记 {human_minutes(summary['actual_minutes'])}"
            f"，加班 {human_minutes(summary['overtime_minutes'])}"
            f"，完成率 {summary['completion']}%\n"
            f"实发 ¥{summary['total_pay']:,.2f}（罚款 -¥{summary['fine_total']:g}）"
            f"，全月预计 ¥{summary['projected_pay']:,.2f}"
        )

    @filter.llm_tool("shift_schedule_image")
    async def ai_schedule_image(self, event: AstrMessageEvent, month: str = "") -> str:
        """生成并单独发送当前说话人的整月排班表图片（排班月历）。

        用户说「排班图」「把排班表发成图片」「看看这个月怎么排」「下个月排班图」时调用本工具。
        只问文字版班表、休息日、班次、工时或工资时，用 shift_get_schedule。

        Args:
            month(string): 月份，格式 YYYY-MM；也可以说本月、下个月、上个月；留空表示本月。
        """
        user = await self._user(event)
        target = parse_month(month)
        _, summary = await self._month_stats(user.user_key, target, user.profile)
        month_profile = _profile_from_summary(user.profile, summary)
        day_dicts = [_day_dict(d) for d in summary["days"]]
        html = render.month_html(
            target, user.nickname, month_profile, day_dicts, summary
        )
        try:
            url = await self.html_render(
                html,
                {},
                return_url=True,
                options=_T2I_OPTIONS,
                umo=event.unified_msg_origin,
            )
        except Exception as exc:  # noqa: BLE001 - t2i 服务未开时回退文本
            logger.warning("AI 排班图渲染失败，回退文本：%s", exc)
            return (
                f"{month_label(target)}排班图生成失败，先把文字班表发你：\n"
                + render.month_text(
                    target, user.nickname, month_profile, day_dicts, summary
                )
            )
        await event.send(MessageChain([Image.fromURL(url)]))
        return f"{month_label(target)}排班图已单独发送。"

    @filter.llm_tool("shift_get_today")
    async def ai_today(self, event: AstrMessageEvent) -> str:
        """查询当前说话人今天上不上班、班次时间、是否正在计时、今日工时。"""
        user = await self._user(event)
        today = date.today()
        _, summary = await self._day_stats(user.user_key, today, user.profile)
        day = summary["days"][0]
        punches = await self.store.list_punches(
            user.user_key, today.isoformat(), today.isoformat()
        )
        opened = next((p for p in punches if p.is_open), None)
        if day.kind == "rest":
            head = f"今天 {today.month}/{today.day} 排休（休息日）"
        else:
            head = (
                f"今天 {today.month}/{today.day} 上班"
                f"，班次 {day.start}-{day.end}（{shift_type_label(user.profile)}）"
                f"，计划 {human_minutes(day.planned_minutes)}"
            )
        if opened:
            elapsed = shift_minutes(opened.start, _now_hhmm(), 0)
            state = f"正在计时，{opened.start} 上班，已过 {human_minutes(elapsed)}"
        elif punches:
            state = f"已下班，今日实记 {human_minutes(day.actual_minutes)}"
        else:
            state = "今天还没有打卡"
        over = (
            f"，其中加班 {human_minutes(day.overtime_minutes)}"
            if day.overtime_minutes
            else ""
        )
        return f"{head}\n{state}{over}"

    @filter.llm_tool("shift_today_attendance")
    async def ai_today_attendance(self, event: AstrMessageEvent) -> str:
        """生成并发送一张「今日考勤」图片给当前说话人：今天上了多久、按小时累计今天已获得多少钱、距下班还剩多久、本月和上月应发与未发、未发合计。用户说「今日考勤」时调用。"""
        user = await self._user(event)
        data = await self._attendance_data(user)
        try:
            url = await self.html_render(
                render.attendance_html(data),
                {},
                return_url=True,
                options=_T2I_ATTENDANCE_OPTIONS,
                umo=event.unified_msg_origin,
            )
            await event.send(MessageChain([Image.fromURL(url)]))
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI 今日考勤图片生成失败：%s", exc)
            return (
                f"今日考勤：今天已上 {data['today_minutes_label']}，"
                f"今天已获得 {data['today_earned']}（{data['today_hourly']} × {data['today_hours_value']}），"
                f"距下班 {data['today_remaining_label']}；"
                f"{data['month_label']}应发 {data['month_due']}、未发 {data['month_unpaid']}；"
                f"所有未发 {data['unpaid_total']}。"
            )
        return "今日考勤图片已单独发送。"

    @filter.llm_tool("shift_get_payroll")
    async def ai_payroll(self, event: AstrMessageEvent) -> str:
        """查询发薪规则、下一笔发薪日、每月应发/已到账/未发合计。"""
        user = await self._user(event)
        return render.payroll_center_text(await self._payroll(user.user_key), user.nickname)

    @filter.llm_tool("shift_set_payroll")
    async def ai_set_payroll(
        self,
        event: AstrMessageEvent,
        mode: str = "",
        payday_day: str = "",
        remind_enabled: str = "",
        remind_time: str = "",
    ) -> str:
        """设置发薪规则与发薪日提醒。只有用户明确要求改发薪规则时调用。

        Args:
            mode(string): month_end=当月月底发；next_month=压一月、次月发。
            payday_day(string): 压一月时，次月几号发，1~31；留空不改。
            remind_enabled(string): 发薪日提醒开/关，填 on 或 off；留空不改。
            remind_time(string): 发薪日提醒时间，HH:MM；留空不改。
        """
        user = await self._user(event)
        fields: dict = {}
        raw_mode = (mode or "").strip().lower()
        if raw_mode:
            if raw_mode in {"月底", "当月", "月终", "month_end"}:
                raw_mode = "month_end"
            elif raw_mode in {"次月", "压一月", "下月", "next_month"}:
                raw_mode = "next_month"
            else:
                return "mode 只能是 month_end（月底发）或 next_month（压一月发）。"
            fields["payday_mode"] = raw_mode
        if payday_day:
            digits = re.sub(r"\D", "", str(payday_day))
            if not digits or not 1 <= int(digits) <= 31:
                return "发薪日需在 1~31 号之间。"
            fields["payday_day"] = int(digits)
        if remind_enabled not in {"", None}:
            fields["payday_remind_enabled"] = str(remind_enabled).strip().lower() in {
                "1",
                "true",
                "on",
                "yes",
                "开",
                "开启",
            }
        if remind_time:
            fields["payday_remind_time"] = normalize_hhmm(remind_time, user.payday_remind_time)
        if not fields:
            return render.payroll_center_text(await self._payroll(user.user_key), user.nickname)
        await self.store.update_profile(user.user_key, **fields)
        payroll = await self._payroll(user.user_key)
        return "已保存发薪规则。\n" + render.payroll_center_text(payroll, user.nickname)

    @filter.llm_tool("shift_mark_salary_paid")
    async def ai_mark_salary_paid(
        self, event: AstrMessageEvent, salary_month: str = "", amount: str = ""
    ) -> str:
        """登记某个月工资已到账；用户明确说「工资到账了」时才调用。

        Args:
            salary_month(string): 工资所属月份 YYYY-MM；留空为本月。
            amount(string): 该月累计到账金额，只填数字；留空表示按剩余未发金额全部登记。
        """
        user = await self._user(event)
        month = _salary_month([salary_month], f"{date.today():%Y-%m}")
        if month > f"{date.today():%Y-%m}":
            return "不能登记未来月份的工资。"
        payroll = await self._payroll(user.user_key)
        entry = next(
            (item for item in payroll.get("entries", []) if item["salary_month"] == month),
            None,
        )
        if entry is None:
            return f"{month_label(month)}没有可登记的应发工资。"
        paid = entry["due"]
        parsed = _number(amount)
        if parsed is not None and float(parsed) > 0:
            paid = min(float(entry["due"]), float(parsed))
        await self.store.save_payout(
            user.user_key,
            month,
            amount=paid,
            paid_on=date.today().isoformat(),
            note="AI 登记到账",
        )
        return f"已登记 {month_label(month)}工资到账 ¥{paid:,.2f}。还有未发 ¥{max(0.0, entry['due'] - paid):,.2f}。"

    @filter.llm_tool("shift_get_hours")
    async def ai_hours(self, event: AstrMessageEvent, range: str = "本月") -> str:
        """查询当前说话人的工时统计：实际工时、计划工时、加班、出勤与缺勤天数。

        Args:
            range(string): 统计范围，只能填 今日 / 本周 / 本月。
        """
        user = await self._user(event)
        raw = (range or "").strip()
        if "今" in raw or "today" in raw.lower():
            scope = "today"
        elif "周" in raw or "week" in raw.lower():
            scope = "week"
        else:
            scope = "month"

        if scope == "today":
            today = date.today()
            _, summary = await self._day_stats(user.user_key, today, user.profile)
            title = f"今日 {today.month}/{today.day}"
        elif scope == "week":
            start, end, _, summary = await self._week_stats(user.user_key, user.profile)
            title = f"本周 {start.month}/{start.day}~{end.month}/{end.day}"
        else:
            month = f"{date.today():%Y-%m}"
            _, summary = await self._month_stats(user.user_key, month, user.profile)
            title = month_label(month)

        return (
            f"{user.nickname} · {title} 工时\n"
            f"实际 {human_minutes(summary['actual_minutes'])}"
            f" / 计划 {human_minutes(summary['planned_minutes'])}"
            f"，加班 {human_minutes(summary['overtime_minutes'])}\n"
            f"出勤 {summary['attended_days']} 天"
            f"，休息 {summary['rest_days']} 天"
            f"，缺勤 {summary['absence_days']} 天"
            f"，完成率 {summary['completion']}%"
        )

    @filter.llm_tool("shift_get_pay")
    async def ai_pay(self, event: AstrMessageEvent, month: str = "") -> str:
        """查询当前说话人某个月的工资：正常工资、加班费、缺勤扣款、实发与全月预测。

        Args:
            month(string): 月份，格式 YYYY-MM；留空表示本月。
        """
        user = await self._user(event)
        target = parse_month(month)
        _, summary = await self._month_stats(user.user_key, target, user.profile)
        if summary["hourly_rate"] <= 0 and summary["monthly_salary"] <= 0:
            return (
                f"{user.nickname} 还没设置工资，算不出来。"
                "可以让 TA 发「/工薪 时薪 22.5」或「/工薪 月薪 5000」，"
                "也可以在本轮直接说月薪/时薪多少，调用 shift_set_pay 写入。"
            )
        if summary["pay_mode"] == "monthly":
            rate_line = (
                f"{summary.get('stage_label', '')}，月薪 ¥{summary['monthly_salary']:,.2f}"
                f"（按本月合同工作日折算 ¥{summary['hourly_rate']:g}/h），"
                f"每日超 {user.profile.standard_hours:g}h 算加班，倍率 ×{user.profile.overtime_rate:g}\n"
            )
            normal_line = f"月薪基数 ¥{summary['normal_pay']:,.2f}\n"
        else:
            rate_line = (
                f"{summary.get('stage_label', '')}，时薪 ¥{summary['hourly_rate']:g}/h，每日超 "
                f"{user.profile.standard_hours:g}h 算加班，倍率 ×{user.profile.overtime_rate:g}\n"
            )
            normal_line = (
                f"正常 {human_minutes(summary['normal_minutes'])} × 时薪"
                f" = ¥{summary['normal_pay']:,.2f}\n"
            )
        return (
            f"{user.nickname} · {month_label(target)} 工资\n"
            + rate_line
            + normal_line
            + f"加班 {human_minutes(summary['overtime_minutes'])} = ¥{summary['overtime_pay']:,.2f}\n"
            f"缺勤 {summary['absence_days']} 天，扣款 ¥{summary['absence_deduction']:,.2f}\n"
            f"罚款 ¥{summary['fine_total']:g}\n"
            + (
                f"固定扣款 ¥{summary['deduction_total']:g}（"
                + "、".join(
                    f"{item['name']} {item['amount']:g}"
                    for item in summary["deduction_items"]
                )
                + "）\n"
                if summary.get("deduction_items")
                else ""
            )
            + f"到手 ¥{summary['total_pay']:,.2f}，实际已挣 ¥{summary['earned_pay']:,.2f}"
            f"，全月干满预计 ¥{summary['projected_pay']:,.2f}"
        )

    @filter.llm_tool("shift_get_annual_income")
    async def ai_annual_income(self, event: AstrMessageEvent, year: str = "") -> str:
        """查询当前说话人某年的实际收入：按已打卡上班的天数累计，不是全月预测。

        Args:
            year(string): 四位年份，如 "2026"；留空表示今年。
        """
        user = await self._user(event)
        target = f"{date.today():%Y}"
        match = re.search(r"20\d{2}", str(year or ""))
        if match:
            target = match.group()
        annual = await self._annual_income(user.user_key, target)
        if not annual["months"]:
            return f"{user.nickname} {target} 年还没有排班或打卡，暂无实际收入。"
        lines = [
            f"{user.nickname} · {target} 年实际收入（已按打卡天数累计，不含未来预测）",
            f"应发 ¥{annual['total']:,.2f}，到手 ¥{annual['net_total']:,.2f}，已上 {annual['paid_days']} 天",
        ]
        for key in ("intern", "regular"):
            bucket = annual["by_stage"].get(key)
            if bucket and (bucket["paid_days"] or bucket["total"]):
                lines.append(
                    f"{bucket['label']}期 ¥{bucket['total']:,.2f}"
                    f"（到手 ¥{bucket['net_total']:,.2f}，{bucket['paid_days']} 天）"
                )
        for row in annual["months"]:
            lines.append(
                f"{row['month_label']} {row['stage_label']}：上 {row['paid_days']} 天，"
                f"应发 ¥{row['earned_pay']:,.2f}，到手 ¥{row['earned_net']:,.2f}"
            )
        return "\n".join(lines)

    @filter.llm_tool("shift_get_profile")
    async def ai_get_profile(self, event: AstrMessageEvent) -> str:
        """查询当前说话人的全部设置：月休方案、班次、扣休、工资口径、上下班提醒、自定义文案和天气城市。"""
        user = await self._user(event)
        profile = user.profile
        pay = (
            f"月薪 ¥{profile.monthly_salary:,.2f}（折算 ¥{profile.hourly_rate:g}/h）"
            if profile.pay_mode == "monthly" and profile.monthly_salary > 0
            else f"时薪 ¥{profile.hourly_rate:g}/h"
        )
        return (
            f"{user.nickname} · 当前设置\n"
            f"月休 {profile.rest_mode} 天，班次 {profile.shift_start}-{profile.shift_end}"
            f"（{shift_type_label(profile)}），每班扣休 {profile.break_minutes} 分钟\n"
            f"计薪 {pay}，每日标准 {profile.standard_hours:g}h，加班倍率 ×{profile.overtime_rate:g}，"
            f"加班提醒 {'开' if user.overtime_alert_enabled else '关'}\n"
            f"提醒 {'开启' if user.remind_enabled else '关闭'}：上班提前 {user.remind_before_start} 分钟，"
            f"下班提前 {user.remind_before_end} 分钟\n"
            f"自定义上班文案：{user.remind_in_text or '未设置（默认温柔版）'}\n"
            f"自定义下班文案：{user.remind_out_text or '未设置（默认温柔版）'}\n"
            f"天气 {'开启' if user.weather_enabled else '关闭'}，城市：{user.weather_city or '未设置'}"
        )

    @filter.llm_tool("shift_set_pay")
    async def ai_set_pay(
        self,
        event: AstrMessageEvent,
        monthly_salary: str = "",
        hourly_rate: str = "",
        pay_mode: str = "",
        overtime_rate: str = "",
        month: str = "",
        employment_stage: str = "",
    ) -> str:
        """设置当前说话人的工资口径：月薪或时薪，可按月份保存并标记实习/正式。
        只有用户本轮明确说出自己的月薪/时薪数字时才调用；没说数字就不要猜。
        说「月薪 5000」就填 monthly_salary；说「时薪 18」就填 hourly_rate；两个都说了就都填，pay_mode 按用户主要口径填。
        说「上个月 3000，这个月 5000」时，month 填对应月份、monthly_salary 填对应数字，调用两次，不要互相覆盖。

        Args:
            monthly_salary(string): 月薪金额，只填数字，如 "5000"；没提到就留空。
            hourly_rate(string): 时薪金额，只填数字，如 "18"；没提到就留空。
            pay_mode(string): 计薪口径，只能填 hourly 或 monthly；留空时按填了哪个自动判断（只填月薪用 monthly，只填时薪用 hourly）。
            overtime_rate(string): 加班倍率，如 "1.5"；留空表示不改。
            month(string): 月份，格式 YYYY-MM，如 "2026-10"；留空表示本月。
            employment_stage(string): 实习 / 正式，或 intern / regular；留空表示不改。
        """
        if not bool(self.config.get("enable_ai_pay", True)):
            return "管理员没有开启「允许 AI 改工资」，这次没有写库。可以让用户发 /工薪 时薪 或 /工薪 月薪。"

        user = await self._user(event)
        updates: dict[str, object] = {}
        notes: list[str] = []
        target_month = _salary_month([month], f"{date.today():%Y-%m}") if str(month or "").strip() else f"{date.today():%Y-%m}"

        salary = _number(monthly_salary) if str(monthly_salary or "").strip() else None
        if salary is not None:
            if salary <= 0 or salary > 1000000:
                return "月薪金额看起来不合理，请用户确认具体数字后再说一次。"
            updates["monthly_salary"] = salary
            notes.append(f"月薪 ¥{salary:,.2f}")
        rate = _number(hourly_rate) if str(hourly_rate or "").strip() else None
        if rate is not None:
            if rate <= 0 or rate > 10000:
                return "时薪金额看起来不合理，请用户确认具体数字后再说一次。"
            updates["hourly_rate"] = rate
            notes.append(f"时薪 ¥{rate:g}/h")
        stage = _parse_stage(employment_stage)
        if stage:
            updates["employment_stage"] = stage
            notes.append(stage_label(stage))
        if not updates:
            return "没有听到具体的月薪或时薪数字，请用户再说一次。"

        mode = str(pay_mode or "").strip().lower()
        if mode in {"monthly", "月薪"}:
            mode = "monthly"
        elif mode in {"hourly", "时薪"}:
            mode = "hourly"
        elif salary is not None:
            mode = "monthly"
        elif rate is not None:
            mode = "hourly"
        else:
            mode = user.profile.pay_mode
        if mode == "monthly" and "monthly_salary" not in updates:
            existing = await self._profile_for_month(user.user_key, target_month, user.profile)
            if existing.monthly_salary <= 0:
                mode = "hourly"
        updates["pay_mode"] = mode

        overtime = _number(overtime_rate) if str(overtime_rate or "").strip() else None
        if overtime is not None:
            updates["overtime_rate"] = max(1.0, min(10.0, overtime))

        updated = await self._save_month_salary(
            user.user_key,
            target_month,
            monthly_salary=updates.get("monthly_salary"),
            hourly_rate=updates.get("hourly_rate"),
            pay_mode=updates.get("pay_mode"),
            employment_stage=updates.get("employment_stage"),
            note="AI 按月份设置",
        )
        if updated is None:
            return "保存失败，请稍后再试。"
        if overtime is not None:
            updated = await self.store.update_profile(user.user_key, overtime_rate=overtime) or updated

        _, summary = await self._month_stats(updated.user_key, target_month, updated.profile)
        basis = (
            f"月薪 ¥{summary['monthly_salary']:,.2f}，折算时薪 ¥{summary['hourly_rate']:g}/h"
            if summary["pay_mode"] == "monthly"
            else f"时薪 ¥{summary['hourly_rate']:g}/h"
        )
        return (
            f"已设置{month_label(target_month)}：{'、'.join(notes)}，"
            f"计薪方式：{'月薪' if summary['pay_mode'] == 'monthly' else '时薪'}，"
            f"身份：{summary.get('stage_label', '')}\n"
            f"{basis}，日薪约 ¥{summary['daily_pay']:,.2f}\n"
            f"该月实发 ¥{summary['total_pay']:,.2f}，实际已挣 ¥{summary['earned_pay']:,.2f}；其他月份工资不变。"
        )

    @filter.llm_tool("shift_set_reminder")
    async def ai_set_reminder(
        self,
        event: AstrMessageEvent,
        enabled: str = "",
        before_start: str = "",
        before_end: str = "",
        custom_in: str = "",
        custom_out: str = "",
        weather_city: str = "",
        weather_enabled: str = "",
        rest_enabled: str = "",
    ) -> str:
        """设置当前说话人的上下班提醒：开关、提前分钟数、自定义上/下班文案、天气城市和天气开关。
        只有用户本轮明确要求修改提醒、文案或天气城市时才调用，不要自行改动。
        文案支持占位符 {nickname} {date} {start} {end} {before} {minutes} {type} {greeting} {punch} {weather} {tomorrow}；
        用户说“恢复默认/清空文案”时，把 custom_in / custom_out 填「清空」。

        Args:
            enabled(string): 提醒总开关，填 开 / 关；不改就留空。
            before_start(string): 上班提前分钟数，0~240；不改就留空。
            before_end(string): 下班提前分钟数，0~240；不改就留空。
            custom_in(string): 上班自定义文案，最多 500 字；填「清空」恢复默认。
            custom_out(string): 下班自定义文案，最多 500 字；填「清空」恢复默认。
            weather_city(string): 天气城市，如「宜宾」；填「关」表示关闭天气提醒。
            weather_enabled(string): 天气开关，填 开 / 关；不改就留空。
            rest_enabled(string): 休息日提醒开关，填 开 / 关；只有今天上班、明天排休才提醒。
        """
        if not bool(self.config.get("enable_ai_remind", True)):
            return "管理员没有开启「允许 AI 改提醒」，这次没有写库。可以让用户发 /提醒 自定义 或 /提醒 城市。"

        user = await self._user(event)
        updates: dict[str, object] = {}
        notes: list[str] = []

        def _bool_value(raw: str) -> bool | None:
            token = str(raw or "").strip().lower()
            if token in {"开", "开启", "启用", "是", "on", "true", "1"}:
                return True
            if token in {"关", "关闭", "禁用", "否", "off", "false", "0"}:
                return False
            return None

        flag = _bool_value(enabled)
        if flag is not None:
            updates["remind_enabled"] = flag
            notes.append("提醒" + ("开启" if flag else "关闭"))
        for key, raw, label in (
            ("remind_before_start", before_start, "上班提前"),
            ("remind_before_end", before_end, "下班提前"),
        ):
            if str(raw or "").strip():
                value = _number(raw)
                if value is None or value < 0 or value > 240:
                    return f"{label}分钟要在 0~240 之间，这次没有写库。"
                updates[key] = int(value)
                notes.append(f"{label} {int(value)} 分钟")

        for key, raw, label in (
            ("remind_in_text", custom_in, "上班文案"),
            ("remind_out_text", custom_out, "下班文案"),
        ):
            text_value = str(raw or "").strip()
            if not text_value:
                continue
            if text_value in {"清空", "清除", "默认", "恢复默认", "reset"}:
                text_value = ""
            if len(text_value) > 500:
                return f"{label}太长了，控制在 500 字以内，这次没有写库。"
            updates[key] = text_value
            notes.append(label + ("已清空" if not text_value else "已更新"))

        city_text = str(weather_city or "").strip()
        if city_text:
            if city_text in {"关", "关闭", "off"}:
                updates["weather_enabled"] = False
                notes.append("天气提醒关闭")
            else:
                if len(city_text) > 40:
                    return "城市名太长了，这次没有写库。"
                updates["weather_city"] = city_text
                updates["weather_enabled"] = True
                notes.append(f"天气城市「{city_text}」")
        weather_flag = _bool_value(weather_enabled)
        if weather_flag is not None:
            updates["weather_enabled"] = weather_flag
            notes.append("天气提醒" + ("开启" if weather_flag else "关闭"))
        rest_flag = _bool_value(rest_enabled)
        if rest_flag is not None:
            updates["rest_reminder_enabled"] = rest_flag
            notes.append("休息日提醒" + ("开启" if rest_flag else "关闭"))

        if not updates:
            return (
                "没有听到要改的提醒内容。可以说「上班提醒提前30分钟」「上班文案改成早安{nickname}」"
                "「天气城市设成宜宾」或「关掉天气提醒」。"
            )
        updated = await self.store.update_profile(user.user_key, **updates)
        if updated is None:
            return "保存失败，请稍后再试。"
        return (
            "已更新：" + "、".join(notes) + "\n"
            f"提醒{'开启' if updated.remind_enabled else '关闭'}，"
            f"上班提前 {updated.remind_before_start} 分钟、下班提前 {updated.remind_before_end} 分钟\n"
            f"天气{'开启' if updated.weather_enabled else '关闭'}，城市：{updated.weather_city or '未设置'}\n"
            "天气由 AstrBot 联网搜索生成；没开联网搜索时天气会静默不显示。"
        )

    @filter.llm_tool("shift_punch")
    async def ai_punch(self, event: AstrMessageEvent, action: str = "状态") -> str:
        """给当前说话人打卡（只有用户本轮明确说要打卡时才调用，不要自行推测）。

        Args:
            action(string): 只能填 上班 / 下班 / 状态。
        """
        user = await self._user(event)
        verb = (action or "状态").strip()
        if "上" in verb:
            verb = "上班"
        elif "下" in verb:
            verb = "下班"
        else:
            verb = "状态"

        if verb == "状态":
            return await self.ai_today(event)

        if not bool(self.config.get("enable_ai_punch", False)):
            return (
                "管理员没有开启「允许 AI 打卡」，这次没有写库。"
                "可以让用户自己发「/打卡 上班」或「/打卡 下班」。"
            )

        today = date.today()
        await self._slot_for_day(user.user_key, today, user.profile)
        punches = await self.store.list_punches(
            user.user_key, today.isoformat(), today.isoformat()
        )

        if verb == "上班":
            opened = next((p for p in punches if p.is_open), None)
            if opened:
                return f"{user.nickname} {opened.start} 已经在计时，不用重复打卡。"
            punch = await self.store.open_punch(
                user.user_key, today.isoformat(), _now_hhmm()
            )
            return f"{user.nickname} 上班打卡 {punch.start}，开始计时。"

        window_start = today - timedelta(days=1)
        recent = await self.store.list_punches(
            user.user_key, window_start.isoformat(), today.isoformat()
        )
        opened = next((p for p in reversed(recent) if p.is_open), None)
        if opened is None:
            return "没找到未结束的上班打卡，先上班打卡或补录后再下班。"
        open_day = date.fromisoformat(opened.day)
        slot = await self._slot_for_day(user.user_key, open_day, user.profile)
        deduct = user.profile.break_minutes if (slot and slot.is_work) else 0
        end = _now_hhmm()
        minutes = shift_minutes(opened.start, end, deduct)
        closed = await self.store.close_open_punch(
            user.user_key, opened.day, end, minutes
        )
        if closed is None:
            return "结算打卡失败，请稍后重试。"
        pay, summary = await self._day_earnings(user, open_day)
        money = (
            f"今天入账 ¥{pay:,.2f}，本月累计实发 ¥{summary['total_pay']:,.2f}"
            if pay > 0
            else "还没设置工资，先把工时记上了"
        )
        return (
            f"{user.nickname} 下班打卡 {closed.end}，本段 {human_minutes(closed.minutes)}"
            f"（{closed.start}-{closed.end}）。{money}。"
        )

    @filter.llm_tool("shift_delete_punch")
    async def ai_delete_punch(self, event: AstrMessageEvent, day_text: str = "") -> str:
        """删除当前说话人某一天的全部打卡记录（不可恢复）。
        只有用户本轮明确说「删掉某天打卡 / 清空某天记录」时才调用；没给具体日期就先问，不要猜。

        Args:
            day_text(string): 日期，如 "11-20" / "2026-11-20" / "今天" / "昨天"。
        """
        if not bool(self.config.get("enable_ai_punch_delete", False)):
            return "管理员没有开启「允许 AI 删除打卡」，这次没有删。可以让用户发 /打卡 删 11-20。"

        user = await self._user(event)
        day = _parse_day_token(day_text)
        if day is None:
            return "没听清具体日期，请用户说清楚要删哪一天，例如 11-20 或 昨天。"
        removed = await self.store.delete_day_punches(user.user_key, day.isoformat())
        if not removed:
            return f"{day.month}/{day.day} 本来就没有打卡记录，不用删除。"
        return (
            f"已删除 {day.month}/{day.day} 的 {removed} 条打卡，"
            "当天工时和工资统计已同步更新。"
        )

    @filter.llm_tool("shift_bulk_punch")
    async def ai_bulk_punch(
        self,
        event: AstrMessageEvent,
        start_date: str = "",
        end_date: str = "",
        start_time: str = "",
        end_time: str = "",
        skip_days: str = "",
        note: str = "",
    ) -> str:
        """批量补录一段日期的打卡记录，用于用户明确要求「把以前的天数补进去」。
        只有用户本轮给出了具体日期范围才调用；不要自行猜测日期或工时。

        Args:
            start_date(string): 起始日期，格式 YYYY-MM-DD 或 MM-DD，如 "2026-11-01"。
            end_date(string): 结束日期，格式同上，如 "2026-11-20"。
            start_time(string): 每天上班时间，如 "08:00"；留空按当前班次上班时间。
            end_time(string): 每天下班时间，如 "20:00"；留空按当前班次下班时间。
            skip_days(string): 要跳过的日期，如 "5,14,18" 或 "5号,14号"；留空表示整段都补。
            note(string): 备注，如 "补历史打卡"；留空自动写「AI 批量补录」。
        """
        if not bool(self.config.get("enable_ai_bulk_punch", True)):
            return (
                "管理员没有开启「允许 AI 批量补录打卡」，这次没有写库。"
                "可以让用户到后台用「导入打卡 CSV」批量导入。"
            )

        user = await self._user(event)
        start = _parse_day_token(start_date)
        end = _parse_day_token(end_date)
        if start is None or end is None:
            return "日期没听清，请用户给出起始和结束日期，例如「11月1号到20号」。"
        if start > end:
            start, end = end, start
        if (end - start).days + 1 > 62:
            return "一次最多补 62 天，请把日期范围缩小一点再说。"

        profile = user.profile
        begin_time = _csv_time(start_time) or profile.shift_start
        finish_time = _csv_time(end_time) or profile.shift_end
        if shift_minutes(begin_time, finish_time, 0) <= 0:
            return "这段上下班时间算出来是 0 工时，请确认时间后再补。"

        today = date.today()
        skip = (_parse_days(skip_days) if str(skip_days or "").strip() else set()) or set()
        note_text = str(note or "").strip() or "AI 批量补录"

        added = 0
        skipped = 0
        duplicated = 0
        invalid = 0
        future = 0
        current = start
        while current <= end:
            if current > today:
                future += 1
                current += timedelta(days=1)
                continue
            if current.day in skip:
                skipped += 1
                current += timedelta(days=1)
                continue
            iso = current.isoformat()
            existing = await self.store.list_punches(user.user_key, iso, iso)
            if any(p.start == begin_time and (p.end or "") == finish_time for p in existing):
                duplicated += 1
                current += timedelta(days=1)
                continue
            slot = await self._slot_for_day(user.user_key, current, profile)
            deduct = profile.break_minutes if (slot and slot.is_work) else 0
            minutes = shift_minutes(begin_time, finish_time, deduct)
            if minutes <= 0:
                invalid += 1
                current += timedelta(days=1)
                continue
            await self.store.add_punch(
                user.user_key, iso, begin_time, finish_time, minutes, note_text
            )
            added += 1
            current += timedelta(days=1)

        month = f"{start:%Y-%m}"
        _, summary = await self._month_stats(user.user_key, month, profile)
        lines = [
            f"已批量补录 {added} 天：{start:%m/%d}~{end:%m/%d}，"
            f"每天 {begin_time}-{finish_time}"
            + (f"（跳过 {skipped} 天）" if skipped else "")
            + (f"，重复跳过 {duplicated} 天" if duplicated else "")
            + (f"，{invalid} 天工时无效" if invalid else "")
            + (f"，{future} 天还没到未补" if future else "")
            + "。",
            f"{month_label(month)}现有出勤 {summary['attended_days']} 天，"
            f"实际 {human_minutes(summary['actual_minutes'])}。",
        ]
        return "\n".join(lines)

    @filter.llm_tool("shift_set_plan")
    async def ai_set_plan(
        self,
        event: AstrMessageEvent,
        month: str = "",
        rest_mode: str = "",
        rest_days: str = "",
        shift_start: str = "",
        shift_end: str = "",
        break_minutes: str = "",
        daily_hours: str = "",
    ) -> str:
        """按用户口头的排班说法写入班表：如「月休4天」「5、14、18、25号休息其他都上班」「每天12小时，8点到20点」。
        只有用户本轮明确说明自己的休息日、班次或每天工时后才调用，不要凭猜测修改排班。

        Args:
            month(string): 月份，格式 YYYY-MM；留空表示本月。
            rest_mode(string): 月休天数，只能填 4 / 6 / 8；留空表示不改。
            rest_days(string): 用户点名的休息日期，如 "5,14,18,25"；留空表示按月休天数自动排。
            shift_start(string): 上班时间，如 "08:00"；留空表示不改。
            shift_end(string): 下班时间，如 "20:00"；留空表示不改。
            break_minutes(string): 每班扣休分钟，如 "60"；留空表示不改。
            daily_hours(string): 每天工作几小时，如 "12"；没给下班时间时用它从上班时间推算下班时间。
        """
        if not bool(self.config.get("enable_ai_plan", True)):
            return "管理员没有开启「允许 AI 改排班」，这次没有写库。可以让用户发 /排班 休息 或 /排班 时间。"

        user = await self._user(event)
        target = parse_month(month)
        last_day = month_bounds(target)[1].day
        profile = user.profile
        updates: dict[str, object] = {}
        notes: list[str] = []

        raw_mode = re.sub(r"\D", "", str(rest_mode or ""))
        if raw_mode:
            mode = int(raw_mode)
            if mode not in _REST_MODES:
                return "月休只支持 4 / 6 / 8 天，这次没有写库。"
            updates["rest_mode"] = mode

        if str(shift_start or "").strip():
            updates["shift_start"] = normalize_hhmm(shift_start, profile.shift_start)
        if str(shift_end or "").strip():
            updates["shift_end"] = normalize_hhmm(shift_end, profile.shift_end)
        raw_break = re.sub(r"\D", "", str(break_minutes or ""))
        if raw_break:
            updates["break_minutes"] = max(0, min(480, int(raw_break)))

        hours = _number(daily_hours)
        start_text = str(updates.get("shift_start") or profile.shift_start)
        break_value = int(updates.get("break_minutes", profile.break_minutes))
        if hours and hours > 0 and not str(shift_end or "").strip():
            span = int(round(hours * 60)) + (break_value if hours >= 4 else 0)
            if span > 24 * 60:
                return "按这个时长算超过 24 小时了，请确认班次时间。"
            updates["shift_end"] = format_hhmm(parse_hhmm(start_text) + span)
            notes.append(f"按每天 {hours:g} 小时推算下班时间")

        days = _parse_days(rest_days) if str(rest_days or "").strip() else []
        if days and any(day > last_day for day in days):
            return f"{month_label(target)}只有 {last_day} 天，休息日期超范围了，这次没有写库。"

        updated = await self.store.update_profile(user.user_key, **updates) if updates else user
        if updated is None:
            return "保存失败，请稍后再试。"
        profile = updated.profile

        if days:
            await self.store.set_month_rests(user.user_key, target, days)
            if len(days) in _REST_MODES and "rest_mode" not in updates:
                refreshed = await self.store.update_profile(user.user_key, rest_mode=len(days))
                if refreshed is not None:
                    profile = refreshed.profile
            notes.append(f"指定休息 {len(days)} 天")
        elif raw_mode:
            await self.store.clear_month_rests(user.user_key, target)

        await self._rebuild_month(user.user_key, target, profile)
        _, summary = await self._month_stats(user.user_key, target, profile)

        rest_days_list = [
            int(day.day[8:10]) for day in summary["days"] if day.kind == "rest"
        ]
        rest_text = "、".join(f"{day}日" for day in rest_days_list[:12]) or "无"
        if len(rest_days_list) > 12:
            rest_text += f" 等 {len(rest_days_list)} 天"
        note_text = f"（{'，'.join(notes)}）" if notes else ""
        return (
            f"{user.nickname} · {month_label(target)} 排班已更新{note_text}\n"
            f"班次 {profile.shift_start}-{profile.shift_end}（{shift_type_label(profile)}）"
            f"，每班 {human_minutes(profile.daily_minutes)}\n"
            f"工作日 {summary['work_days']} 天，休息日 {summary['rest_days']} 天（{rest_text}）\n"
            f"计划 {human_minutes(summary['planned_minutes'])}，实发 ¥{summary['total_pay']:,.2f}"
        )

    @filter.llm_tool("shift_add_fine")
    async def ai_add_fine(
        self,
        event: AstrMessageEvent,
        amount: str = "",
        reason: str = "",
        month: str = "",
    ) -> str:
        """记录用户本月的罚款金额，并自动从当月实发工资里扣掉。
        只有用户本轮明确说自己被罚了多少钱时才调用；金额不确定时先问清楚，不要自己编。

        Args:
            amount(string): 罚款金额，只填数字，如 "200"。
            reason(string): 罚款原因，如 "迟到"；不知道就留空。
            month(string): 月份，格式 YYYY-MM；留空表示本月。
        """
        if not bool(self.config.get("enable_ai_fine", True)):
            return "管理员没有开启「允许 AI 记罚款」，这次没有写库。可以让用户发 /排班 罚款 200 原因。"

        user = await self._user(event)
        target = parse_month(month)
        value = _number(amount)
        if value is None or value <= 0:
            return "没听清罚款金额，请用户再说一次具体数字。"
        if value > 100000:
            return "罚款金额看起来不合理，请用户确认后再说一次。"

        await self.store.add_fine(
            user.user_key, target, value, str(reason or "").strip() or "罚款"
        )
        _, summary = await self._month_stats(user.user_key, target, user.profile)
        return (
            f"已记罚款 ¥{value:g}（{str(reason or '').strip() or '罚款'}）\n"
            f"{month_label(target)}累计罚款 ¥{summary['fine_total']:g}\n"
            f"正常+加班 ¥{summary['gross_pay']:,.2f}，扣罚款后实发 ¥{summary['total_pay']:,.2f}"
        )

    @filter.llm_tool("shift_get_deductions")
    async def ai_get_deductions(self, event: AstrMessageEvent, month: str = "") -> str:
        """查询当前说话人某个月的固定扣款明细与扣款后到手工资。

        Args:
            month(string): 月份，格式 YYYY-MM；留空表示本月。
        """
        user = await self._user(event)
        target = parse_month(month)
        items = await self.store.list_deductions(user.user_key, target)
        _, summary = await self._month_stats(user.user_key, target, user.profile)
        if not items:
            return (
                f"{month_label(target)}还没有固定扣款。\n"
                "如果用户提到社保、水电、餐费、房租等每月固定支出，可以帮用户添加。"
            )
        lines = [f"{month_label(target)}固定扣款："]
        for item in items:
            tag = "每月" if item["repeat_monthly"] else "仅本月"
            lines.append(
                f"#{item['id']} {item['name']} ¥{item['amount']:g}（{tag}）"
            )
        lines.append(
            f"合计 -¥{summary['deduction_total']:g}；"
            f"扣款前 ¥{summary['pay_before_deductions']:,.2f}，"
            f"到手 ¥{summary['total_pay']:,.2f}"
        )
        return "\n".join(lines)

    @filter.llm_tool("shift_add_deduction")
    async def ai_add_deduction(
        self,
        event: AstrMessageEvent,
        name: str,
        amount: str,
        repeat: str = "",
        month: str = "",
    ) -> str:
        """给用户添加一笔每月扣款，标签完全由用户决定，例如房租、社保、水电费、餐费。
        只有用户明确说出标签和金额后才调用；金额不清楚时先问用户，不要猜。

        Args:
            name(string): 用户自定义的扣款标签，如 "房租"、"社保"、"水电费"、"餐费"。
            amount(string): 金额数字，如 "1500"、"120"。
            repeat(string): 是否每月重复，填 "每月" 或 "本月一次"；留空默认每月。
            month(string): 起始月份 YYYY-MM；留空表示本月。
        """
        if not bool(self.config.get("enable_ai_deduction", True)):
            return "管理员没有开启「允许 AI 管理扣款」，这次没有写库。可以让用户发 /扣款 添加 标签 金额。"
        user = await self._user(event)
        label = str(name or "").strip()[:20]
        if not label:
            return "扣款标签为空，请用户给一个名称，例如房租、社保、水电费。"
        value = _number(amount)
        if value is None or value <= 0:
            return "没听清扣款金额，请用户再说一次具体数字。"
        if value > 1000000:
            return "金额看起来不合理，请用户确认后再说一次。"
        target = parse_month(month)
        raw_repeat = str(repeat or "").strip()
        monthly = not any(
            word in raw_repeat for word in ("本月一次", "单次", "一次", "只扣本月")
        )
        item = await self.store.add_deduction(
            user.user_key, label, value, target, repeat_monthly=monthly
        )
        _, summary = await self._month_stats(user.user_key, target, user.profile)
        return (
            f"已添加扣款「{item['name']}」¥{value:g}（{'每月固定' if monthly else '仅本月一次'}）\n"
            f"{month_label(target)}扣款合计 ¥{summary['deduction_total']:g}，"
            f"到手 ¥{summary['total_pay']:,.2f}"
        )

    @filter.llm_tool("shift_delete_deduction")
    async def ai_delete_deduction(
        self, event: AstrMessageEvent, target: str, month: str = ""
    ) -> str:
        """删除用户的一笔扣款，可以按编号或标签删除。

        Args:
            target(string): 扣款编号（如 "3"）或标签（如 "房租"）。
            month(string): 月份 YYYY-MM；留空表示本月。
        """
        if not bool(self.config.get("enable_ai_deduction", True)):
            return "管理员没有开启「允许 AI 管理扣款」，这次没有删除。可以让用户发 /扣款 删除 标签。"
        user = await self._user(event)
        token = str(target or "").strip()
        current = parse_month(month)
        if not token:
            return "请用户说明要删除哪一笔扣款，可以给编号或标签。"
        if token.isdigit():
            removed = await self.store.delete_deduction(user.user_key, int(token))
            if not removed:
                return f"没有找到编号 {token} 的扣款。"
            return f"已删除扣款 #{token}。"
        item = await self.store.find_deduction_by_name(user.user_key, token, current)
        if item is None:
            return f"没有找到标签「{token}」的扣款。"
        await self.store.delete_deduction(user.user_key, item["id"])
        return f"已删除扣款「{token}」¥{item['amount']:g}。"

    @filter.llm_tool("shift_set_overtime")
    async def ai_set_overtime(
        self,
        event: AstrMessageEvent,
        multiplier: str = "",
        standard_hours: str = "",
        alert: str = "",
    ) -> str:
        """设置加班倍率、每日标准工时，以及是否开启加班提醒。
        只有用户明确提到加班费倍率、8 小时标准或开关加班提醒时才调用。

        Args:
            multiplier(string): 加班倍率，如 "1.5"；留空不改。
            standard_hours(string): 每日标准工时，如 "8"；留空不改。
            alert(string): 加班提醒开关，填 "开" 或 "关"；留空不改。
        """
        if not bool(self.config.get("enable_ai_pay", True)):
            return "管理员没有开启「允许 AI 改工资设置」，这次没有写库。可以让用户发 /工薪 加班 1.5。"
        user = await self._user(event)
        fields: dict[str, object] = {}
        value = _number(multiplier)
        if value is not None:
            if not 1 <= value <= 10:
                return "加班倍率要在 1 ~ 10 之间。"
            fields["overtime_rate"] = value
        hours = _number(standard_hours)
        if hours is not None:
            if not 1 <= hours <= 24:
                return "每日标准工时要在 1 ~ 24 小时之间。"
            fields["standard_hours"] = hours
        action = str(alert or "").strip()
        if action in {"开", "开启", "on"}:
            fields["overtime_alert_enabled"] = True
        elif action in {"关", "关闭", "off"}:
            fields["overtime_alert_enabled"] = False
        if not fields:
            return (
                f"当前加班 ×{user.profile.overtime_rate:g}，每日标准 "
                f"{user.profile.standard_hours:g}h，加班提醒"
                f"{'开' if user.overtime_alert_enabled else '关'}。"
            )
        updated = await self.store.update_profile(user.user_key, **fields)
        if updated is None:
            return "保存失败，请稍后再试。"
        month = f"{date.today():%Y-%m}"
        _, summary = await self._month_stats(updated.user_key, month, updated.profile)
        return (
            f"已更新：加班 ×{updated.profile.overtime_rate:g}，每日标准 "
            f"{updated.profile.standard_hours:g}h，加班提醒"
            f"{'开' if updated.overtime_alert_enabled else '关'}。\n"
            f"本月已加班 {human_minutes(summary['overtime_minutes'])}，"
            f"加班时薪约 ¥{summary['hourly_rate'] * updated.profile.overtime_rate:g}/h。"
        )

    # ------------------------------------------------------------------ #
    # WebUI API
    # ------------------------------------------------------------------ #
    async def _snapshot(self, user_key: str, month: str) -> dict:
        record = await self.store.get_user(user_key)
        if record is None:
            return {}
        _, summary = await self._month_stats(user_key, month, record.profile)
        annual = await self._annual_income(user_key, month[:4])
        payroll = await self._payroll(user_key)
        return {
            "active": record.to_dict(),
            "month": month,
            "month_label": month_label(month),
            "days": [_day_dict(day) for day in summary["days"]],
            "summary": _summary_dict(summary),
            "annual": annual,
            "payroll": payroll,
        }

    async def api_state(self):
        month = parse_month(request.query.get("month", ""))
        users = await self.store.list_users()
        if not users:
            return json_response(
                {
                    "users": [],
                    "active": None,
                    "month": month,
                    "month_label": month_label(month),
                    "days": [],
                    "summary": None,
                    "annual": None,
                    "payroll": None,
                }
            )
        wanted = request.query.get("user", "")
        active = next((u for u in users if u.user_key == wanted), users[0])
        snapshot = await self._snapshot(active.user_key, month)
        return json_response(
            {
                "users": [user.to_dict() for user in users],
                "month": month,
                "month_label": month_label(month),
                **snapshot,
            }
        )

    async def api_profile(self):
        payload = await request.json(default={})
        user_key = str(payload.get("user") or "")
        record = await self.store.get_user(user_key)
        if record is None:
            return error_response("找不到这个用户", status_code=404)

        fields: dict = {}
        try:
            if "hourly_rate" in payload:
                rate = float(payload["hourly_rate"])
                if not 0 <= rate <= 10000:
                    return error_response("时薪需在 0 ~ 10000 之间")
                fields["hourly_rate"] = rate
            if "monthly_salary" in payload:
                salary = float(payload["monthly_salary"])
                if not 0 <= salary <= 1000000:
                    return error_response("月薪需在 0 ~ 1000000 之间")
                fields["monthly_salary"] = salary
            if "pay_mode" in payload:
                mode = str(payload["pay_mode"] or "").lower()
                if mode not in {"hourly", "monthly"}:
                    return error_response("计薪方式只支持 hourly / monthly")
                fields["pay_mode"] = mode
            if "employment_stage" in payload:
                stage = str(payload["employment_stage"] or "").lower()
                if stage not in {"intern", "regular"}:
                    return error_response("工资阶段只支持 intern（实习）/ regular（正式）")
                fields["employment_stage"] = stage
            if "overtime_rate" in payload:
                overtime = float(payload["overtime_rate"])
                if not 1 <= overtime <= 10:
                    return error_response("加班倍率需在 1 ~ 10 之间")
                fields["overtime_rate"] = overtime
            if "standard_hours" in payload:
                standard = float(payload["standard_hours"])
                if not 1 <= standard <= 24:
                    return error_response("每日标准工时需在 1 ~ 24 小时之间")
                fields["standard_hours"] = standard
            if "overtime_alert_enabled" in payload:
                fields["overtime_alert_enabled"] = bool(payload["overtime_alert_enabled"])
            if "rest_reminder_enabled" in payload:
                fields["rest_reminder_enabled"] = bool(payload["rest_reminder_enabled"])
            if "shift_start" in payload:
                fields["shift_start"] = normalize_hhmm(payload["shift_start"], "09:00")
            if "shift_end" in payload:
                fields["shift_end"] = normalize_hhmm(payload["shift_end"], "18:00")
            if "break_minutes" in payload:
                break_minutes = int(payload["break_minutes"])
                if not 0 <= break_minutes <= 480:
                    return error_response("扣休分钟需在 0 ~ 480 之间")
                fields["break_minutes"] = break_minutes
            if "rest_mode" in payload:
                mode = int(payload["rest_mode"])
                if mode not in _REST_MODES:
                    return error_response("月休只支持 4 / 6 / 8 天")
                fields["rest_mode"] = mode
            if "prefer_weekend" in payload:
                fields["prefer_weekend"] = bool(payload["prefer_weekend"])
            if "absence_deduction" in payload:
                fields["absence_deduction"] = bool(payload["absence_deduction"])
            if "rest_day_as_overtime" in payload:
                fields["rest_day_as_overtime"] = bool(payload["rest_day_as_overtime"])
            if "remind_enabled" in payload:
                fields["remind_enabled"] = bool(payload["remind_enabled"])
            if "remind_before_start" in payload:
                before_start = int(payload["remind_before_start"])
                if not 0 <= before_start <= 240:
                    return error_response("上班提醒提前分钟需在 0 ~ 240 之间")
                fields["remind_before_start"] = before_start
            if "remind_before_end" in payload:
                before_end = int(payload["remind_before_end"])
                if not 0 <= before_end <= 240:
                    return error_response("下班提醒提前分钟需在 0 ~ 240 之间")
                fields["remind_before_end"] = before_end
            if "remind_in_text" in payload:
                fields["remind_in_text"] = str(payload["remind_in_text"] or "").strip()[:500]
            if "remind_out_text" in payload:
                fields["remind_out_text"] = str(payload["remind_out_text"] or "").strip()[:500]
            if "weather_city" in payload:
                fields["weather_city"] = str(payload["weather_city"] or "").strip()[:40]
            if "weather_enabled" in payload:
                fields["weather_enabled"] = bool(payload["weather_enabled"])
        except (TypeError, ValueError):
            return error_response("参数格式不正确")

        month = parse_month(payload.get("month", ""))
        salary_keys = {"hourly_rate", "monthly_salary", "pay_mode", "employment_stage"}
        salary_fields = {key: fields.pop(key) for key in list(fields) if key in salary_keys}

        if salary_fields:
            if salary_fields.get("monthly_salary", 0) > 0 and "pay_mode" not in salary_fields:
                salary_fields["pay_mode"] = "monthly"
            month_profile = await self._profile_for_month(user_key, month, record.profile)
            salary_value = salary_fields.get("monthly_salary", month_profile.monthly_salary)
            if salary_fields.get("pay_mode") == "monthly" and float(salary_value or 0) <= 0:
                return error_response("按月薪计薪要先填月薪金额")
            await self._save_month_salary(
                user_key,
                month,
                monthly_salary=salary_fields.get("monthly_salary"),
                hourly_rate=salary_fields.get("hourly_rate"),
                pay_mode=salary_fields.get("pay_mode"),
                employment_stage=salary_fields.get("employment_stage"),
                note="后台按月份保存",
            )

        if fields:
            await self.store.update_profile(user_key, **fields)

        rebuild_keys = {
            "shift_start",
            "shift_end",
            "break_minutes",
            "rest_mode",
            "prefer_weekend",
        }
        if rebuild_keys & fields.keys():
            updated = await self.store.get_user(user_key)
            if updated:
                await self._rebuild_month(user_key, month, updated.profile)

        return json_response({"saved": True, **await self._snapshot(user_key, month)})

    async def api_payroll_save(self):
        payload = await request.json(default={})
        user_key = str(payload.get("user") or "")
        record = await self.store.get_user(user_key)
        if record is None:
            return error_response("找不到这个用户", status_code=404)
        mode = str(payload.get("payday_mode") or "month_end").lower()
        if mode not in {"month_end", "next_month"}:
            return error_response("发薪规则只支持 month_end / next_month")
        try:
            payday_day = int(payload.get("payday_day", 15) or 15)
        except (TypeError, ValueError):
            return error_response("发薪日需为 1 ~ 31 的整数")
        if not 1 <= payday_day <= 31:
            return error_response("发薪日需为 1 ~ 31 的整数")
        remind_enabled = bool(payload.get("payday_remind_enabled", True))
        remind_time = normalize_hhmm(
            payload.get("payday_remind_time") or record.payday_remind_time, "17:00"
        )
        await self.store.update_profile(
            user_key,
            payday_mode=mode,
            payday_day=payday_day,
            payday_remind_enabled=remind_enabled,
            payday_remind_time=remind_time,
        )
        month = parse_month(payload.get("month", ""))
        return json_response({"saved": True, **await self._snapshot(user_key, month)})

    async def api_payroll_pay(self):
        payload = await request.json(default={})
        user_key = str(payload.get("user") or "")
        record = await self.store.get_user(user_key)
        if record is None:
            return error_response("找不到这个用户", status_code=404)
        month = parse_month(
            _salary_month([str(payload.get("salary_month") or "")]), f"{date.today():%Y-%m}"
        )
        if month > f"{date.today():%Y-%m}":
            return error_response("不能给未来月份登记到账")
        payroll = await self._payroll(user_key)
        entry = next(
            (item for item in payroll.get("entries", []) if item["salary_month"] == month),
            None,
        )
        if entry is None:
            return error_response("这个月还没有应发工资", status_code=404)
        amount = entry["due"]
        if "amount" in payload and payload.get("amount") not in {None, ""}:
            try:
                amount = float(payload["amount"])
            except (TypeError, ValueError):
                return error_response("到账金额格式不正确")
        amount = round(max(0.0, min(float(entry["due"]), amount)), 2)
        paid_on = _parse_day_token(str(payload.get("paid_on") or "")) or date.today()
        if paid_on > date.today():
            return error_response("到账日期不能是未来")
        await self.store.save_payout(
            user_key,
            month,
            amount=amount,
            paid_on=paid_on.isoformat(),
            note=str(payload.get("note") or "后台登记到账")[:200],
        )
        return json_response(
            {
                "saved": True,
                "salary_month": month,
                **await self._snapshot(
                    user_key, parse_month(payload.get("month", ""))
                ),
            }
        )

    async def api_payroll_unpay(self):
        payload = await request.json(default={})
        user_key = str(payload.get("user") or "")
        month = parse_month(
            _salary_month([str(payload.get("salary_month") or "")]), f"{date.today():%Y-%m}"
        )
        await self.store.delete_payout(user_key, month)
        return json_response(
            {
                "saved": True,
                **await self._snapshot(
                    user_key, parse_month(payload.get("month", ""))
                ),
            }
        )

    async def api_mode(self):
        payload = await request.json(default={})
        user_key = str(payload.get("user") or "")
        try:
            mode = int(payload.get("rest_mode"))
        except (TypeError, ValueError):
            return error_response("月休只支持 4 / 6 / 8 天")
        if mode not in _REST_MODES:
            return error_response("月休只支持 4 / 6 / 8 天")
        record = await self.store.update_profile(user_key, rest_mode=mode)
        if record is None:
            return error_response("找不到这个用户", status_code=404)
        month = parse_month(payload.get("month", ""))
        await self._rebuild_month(user_key, month, record.profile)
        return json_response({"saved": True, **await self._snapshot(user_key, month)})

    async def api_regenerate(self):
        payload = await request.json(default={})
        user_key = str(payload.get("user") or "")
        record = await self.store.get_user(user_key)
        if record is None:
            return error_response("找不到这个用户", status_code=404)
        month = parse_month(payload.get("month", ""))
        await self._rebuild_month(user_key, month, record.profile)
        return json_response(await self._snapshot(user_key, month))

    async def api_punch(self):
        payload = await request.json(default={})
        user_key = str(payload.get("user") or "")
        record = await self.store.get_user(user_key)
        if record is None:
            return error_response("找不到这个用户", status_code=404)
        day = _parse_day_token(str(payload.get("date") or ""))
        if day is None:
            return error_response("日期格式应为 YYYY-MM-DD")
        if day > date.today():
            return error_response("不能给未来日期补打卡")
        start = normalize_hhmm(payload.get("start"), "09:00")
        end = normalize_hhmm(payload.get("end"), "18:00")
        slot = await self._slot_for_day(user_key, day, record.profile)
        deduct = record.profile.break_minutes if (slot and slot.is_work) else 0
        minutes = shift_minutes(start, end, deduct)
        if minutes <= 0:
            return error_response("这段上下班时间的有效工时为 0")
        await self.store.add_punch(
            user_key, day.isoformat(), start, end, minutes, "WebUI 补录"
        )
        return json_response(
            await self._snapshot(user_key, f"{day:%Y-%m}")
        )

    async def api_punch_delete(self):
        payload = await request.json(default={})
        user_key = str(payload.get("user") or "")
        record = await self.store.get_user(user_key)
        if record is None:
            return error_response("找不到这个用户", status_code=404)
        month = parse_month(payload.get("month", ""))
        punch_id = payload.get("id")
        if punch_id not in (None, ""):
            try:
                removed = await self.store.delete_punch(user_key, int(punch_id))
            except (TypeError, ValueError):
                return error_response("打卡 ID 不合法")
            if not removed:
                return error_response("这条打卡不存在", status_code=404)
            return json_response(await self._snapshot(user_key, month))
        day = _parse_day_token(str(payload.get("date") or ""))
        if day is None:
            return error_response("日期格式应为 YYYY-MM-DD")
        await self.store.delete_day_punches(user_key, day.isoformat())
        return json_response(await self._snapshot(user_key, month))

    async def api_day_mark(self):
        """标记某天未上班/请假，或恢复为工作日。"""
        payload = await request.json(default={})
        user_key = str(payload.get("user") or "")
        record = await self.store.get_user(user_key)
        if record is None:
            return error_response("找不到这个用户", status_code=404)
        day = _parse_day_token(str(payload.get("date") or ""))
        if day is None:
            return error_response("日期格式应为 YYYY-MM-DD")
        month = f"{day:%Y-%m}"
        action = str(payload.get("action") or "leave").strip().lower()
        if action not in {"leave", "clear"}:
            return error_response("action 只支持 leave / clear")
        slot = await self._slot_for_day(user_key, day, record.profile)
        if slot is None:
            return error_response("这一天不在排班表里", status_code=404)
        if action == "clear":
            await self.store.clear_leave_days(user_key, month, [day.day])
            await self._rebuild_month(user_key, month, record.profile)
            return json_response(await self._snapshot(user_key, month))
        if slot.kind == "rest":
            return error_response("这天本来就是休息日，不用标记未上班/请假")
        if await self.store.list_punches(user_key, day.isoformat(), day.isoformat()):
            return error_response("这天已有打卡记录，先删掉打卡再标记未上班/请假")
        await self.store.set_leave_days(user_key, month, [day.day])
        return json_response(await self._snapshot(user_key, month))

    # ------------------------------------------------------------------ #
    # WebUI 每月扣款
    # ------------------------------------------------------------------ #
    async def api_deduction_save(self):
        payload = await request.json(default={})
        user_key = str(payload.get("user") or "")
        record = await self.store.get_user(user_key)
        if record is None:
            return error_response("找不到这个用户", status_code=404)
        name = str(payload.get("name") or "").strip()[:20]
        if not name:
            return error_response("扣款标签不能为空")
        try:
            amount = float(payload.get("amount"))
        except (TypeError, ValueError):
            return error_response("扣款金额要填数字")
        if not 0 < amount <= 1000000:
            return error_response("扣款金额需在 0 ~ 1000000 之间")
        month = parse_month(payload.get("month", ""))
        repeat = bool(payload.get("repeat_monthly", True))
        deduction_id = payload.get("id")
        try:
            if deduction_id not in (None, ""):
                updated = await self.store.update_deduction(
                    user_key,
                    int(deduction_id),
                    name=name,
                    amount=amount,
                    repeat_monthly=repeat,
                    month=month,
                )
                if updated is None:
                    return error_response("这笔扣款不存在", status_code=404)
            else:
                await self.store.add_deduction(
                    user_key, name, amount, month, repeat_monthly=repeat
                )
        except ValueError as exc:
            return error_response(str(exc))
        return json_response(await self._snapshot(user_key, month))

    async def api_deduction_delete(self):
        payload = await request.json(default={})
        user_key = str(payload.get("user") or "")
        record = await self.store.get_user(user_key)
        if record is None:
            return error_response("找不到这个用户", status_code=404)
        try:
            deduction_id = int(payload.get("id"))
        except (TypeError, ValueError):
            return error_response("扣款 ID 不正确")
        if not await self.store.delete_deduction(user_key, deduction_id):
            return error_response("这笔扣款不存在", status_code=404)
        month = parse_month(payload.get("month", ""))
        return json_response(await self._snapshot(user_key, month))

    # ------------------------------------------------------------------ #
    # WebUI 外观：自定义背景
    # ------------------------------------------------------------------ #
    _BG_MAX_CHARS = 6_500_000

    async def api_background(self):
        raw = await self.store.get_setting("background", "")
        data: dict = {}
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    data = parsed
            except json.JSONDecodeError:
                data = {}
        image = str(data.get("image") or "")
        if not (image.startswith("data:image/") or image.startswith("preset:")):
            image = ""
        return json_response(
            {
                "image": image,
                "blur": _clamp_float(data.get("blur"), 0.0, 30.0, 8.0),
                "dim": _clamp_float(data.get("dim"), 0.0, 0.85, 0.45),
            }
        )

    async def api_background_save(self):
        payload = await request.json(default={})
        image = str(payload.get("image") or "")
        if image and not (
            image.startswith("data:image/") or image.startswith("preset:")
        ):
            return error_response("背景图格式不支持，请上传图片或选择预设")
        if len(image) > self._BG_MAX_CHARS:
            return error_response("背景图太大了，换一张小一点的图片")
        try:
            blur = max(0.0, min(30.0, float(payload.get("blur", 8))))
            dim = max(0.0, min(0.85, float(payload.get("dim", 0.45))))
        except (TypeError, ValueError):
            return error_response("背景参数不正确")
        await self.store.set_setting(
            "background",
            json.dumps({"image": image, "blur": blur, "dim": dim}, ensure_ascii=False),
        )
        return json_response(
            {"saved": True, "image": image, "blur": blur, "dim": dim}
        )

    # ------------------------------------------------------------------ #
    # WebUI 数据导入 / 导出
    # ------------------------------------------------------------------ #
    async def api_data_export(self):
        """导出整库备份：七个表原样打包成 JSON，前端直接下载成文件。"""
        backup = await self.store.export_all()
        return json_response(backup)

    async def api_data_import(self):
        """导入整库备份；replace=true 时先清空现有数据（前端会二次确认）。"""
        payload = await request.json(default={})
        backup = None
        if isinstance(payload, dict):
            backup = payload.get("payload") or payload.get("data")
            if backup is None and "tables" in payload:
                backup = payload
        if not isinstance(backup, dict):
            return error_response("没有收到备份内容，或文件不是排班工坊的备份")
        replace = bool(payload.get("replace", False))
        try:
            counts = await self.store.import_all(backup, replace=replace)
        except ValueError as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"排班工坊：备份导入失败：{exc}")
            return error_response("导入失败，现有数据没有被改动")
        return json_response(
            {
                "ok": True,
                "replace": replace,
                "counts": counts,
                "months": _backup_months(backup),
            }
        )

    async def api_punches_import(self):
        """导入打卡 CSV：日期,上班,下班（可选备注），给已有考勤表的人一次性搬进来。"""
        payload = await request.json(default={})
        user_key = str(payload.get("user") or "")
        record = await self.store.get_user(user_key)
        if record is None:
            return error_response("找不到这个用户", status_code=404)
        text = payload.get("csv")
        if not isinstance(text, str) or not text.strip():
            return error_response("CSV 内容为空")
        rows = _read_csv_rows(text)
        if not rows:
            return error_response("CSV 里没读到数据行")

        imported = 0
        skipped = 0
        invalid = 0
        cache: dict[str, set[tuple[str, str]]] = {}
        month_counts: dict[str, int] = {}
        first_day: str | None = None
        last_day: str | None = None
        bad_rows: list[str] = []
        for row in rows[:2000]:
            if len(row) < 3:
                invalid += 1
                if len(bad_rows) < 3:
                    bad_rows.append(" / ".join(cell.strip() for cell in row))
                continue
            day = _csv_day(row[0])
            start = _csv_time(row[1])
            end = _csv_time(row[2])
            if day is None or start is None or end is None:
                invalid += 1
                if len(bad_rows) < 3:
                    bad_rows.append(" / ".join(cell.strip() for cell in row[:3]))
                continue
            iso = day.isoformat()
            if first_day is None or iso < first_day:
                first_day = iso
            if last_day is None or iso > last_day:
                last_day = iso
            month_counts[iso[:7]] = month_counts.get(iso[:7], 0) + 1
            if iso not in cache:
                existing = await self.store.list_punches(user_key, iso, iso)
                cache[iso] = {
                    (punch.start, punch.end or "") for punch in existing
                }
            if (start, end) in cache[iso]:
                skipped += 1
                continue
            slot = await self._slot_for_day(user_key, day, record.profile)
            deduct = record.profile.break_minutes if (slot and slot.is_work) else 0
            minutes = shift_minutes(start, end, deduct)
            if minutes <= 0:
                invalid += 1
                if len(bad_rows) < 3:
                    bad_rows.append(f"{iso} {start}-{end}（有效工时为 0）")
                continue
            note = row[3].strip() if len(row) > 3 else "CSV 导入"
            await self.store.add_punch(
                user_key, iso, start, end, minutes, note or "CSV 导入"
            )
            cache[iso].add((start, end))
            imported += 1
        return json_response(
            {
                "ok": True,
                "user": user_key,
                "imported": imported,
                "skipped": skipped,
                "invalid": invalid,
                "first_day": first_day,
                "last_day": last_day,
                "months": [
                    {"month": month, "count": count}
                    for month, count in sorted(month_counts.items())
                ],
                "bad_rows": bad_rows,
            }
        )

    async def terminate(self):
        """插件卸载/停用时的收尾。"""
        task = self._reminder_task
        self._reminder_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"排班工坊：提醒任务收尾异常：{exc}")
        logger.info("排班工坊插件已停止。")
