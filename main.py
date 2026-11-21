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
from datetime import date, datetime, timedelta
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
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
    shift_type_label,
    summarize,
    week_range,
)
from .store import ShiftStore, UserRecord

PLUGIN_NAME = "astrbot_plugin_shift_board"
_REST_MODES = (4, 6, 8)
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


def _day_dict(day) -> dict:
    return {
        "day": day.day,
        "weekday": day.weekday,
        "kind": day.kind,
        "start": day.start,
        "end": day.end,
        "planned_minutes": day.planned_minutes,
        "actual_minutes": day.actual_minutes,
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


def _summary_dict(summary: dict) -> dict:
    return {key: value for key, value in summary.items() if key != "days"}


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
            ("background", self.api_background, ["GET"], "读取页面背景设置"),
            ("background/save", self.api_background_save, ["POST"], "保存页面背景设置"),
            ("data/export", self.api_data_export, ["GET"], "导出整库备份 JSON"),
            ("data/import", self.api_data_import, ["POST"], "导入整库备份"),
            ("punches/import", self.api_punches_import, ["POST"], "导入打卡 CSV"),
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

    async def _user(self, event: AstrMessageEvent) -> UserRecord:
        uid = str(event.get_sender_id() or "unknown").strip() or "unknown"
        platform = (event.get_platform_name() or "").strip() or "unknown"
        # 数据分区键带上平台前缀：同一个人在不同平台、或不同人数字 ID 撞车时不会串数据
        user_key = f"{platform}:{uid}"
        nickname = event.get_sender_name() or uid
        self._start_reminder_task()
        return await self.store.ensure_user(
            user_key,
            uid=uid,
            platform=platform,
            nickname=nickname,
            defaults=self._default_profile(),
            umo=str(getattr(event, "unified_msg_origin", "") or ""),
            remind_defaults=self._reminder_defaults(),
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
        slots = build_month_schedule_for(month, profile, rest_override=rests)
        await self.store.save_schedule(user_key, month, slots)
        return slots

    async def _month_stats(
        self, user_key: str, month: str, profile: Profile
    ) -> tuple[list[DaySlot], dict]:
        slots = await self._ensure_month(user_key, month, profile)
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
            month_complete=month < current_month,
            fine_total=await self.store.fine_total(user_key, month),
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
        if profile.hourly_rate <= 0 and profile.monthly_salary <= 0:
            return 0.0, summary
        rate = summary["hourly_rate"]
        overtime = info.overtime_minutes / 60 * rate * profile.overtime_rate
        if summary["pay_mode"] == "monthly" and summary["work_days"] > 0:
            base = (
                profile.monthly_salary / summary["work_days"]
                if info.kind == "work" and info.actual_minutes > 0
                else 0.0
            )
            return base + overtime, summary
        normal = info.normal_minutes / 60 * rate
        return normal + overtime, summary

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
            if not user.remind_enabled or not user.umo:
                continue
            if user.remind_before_start <= 0 and user.remind_before_end <= 0:
                continue
            try:
                await self._check_one_reminder(user, now)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"排班工坊：{user.user_key} 的提醒检查失败：{exc}"
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
                            ),
                        )

    def _in_window(self, now: datetime, target: datetime) -> bool:
        return target <= now < target + timedelta(minutes=self._REMIND_GRACE_MINUTES)

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

    @shift_group.command("帮助", alias={"help"})
    async def shift_help(self, event: AstrMessageEvent):
        """查看全部指令"""
        yield event.plain_result(render.help_text())

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
                month, user.nickname, user.profile, [_day_dict(d) for d in summary["days"]], summary
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
                month, user.nickname, user.profile, [_day_dict(d) for d in summary["days"]], summary
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
                month, updated.nickname, updated.profile, [_day_dict(d) for d in summary["days"]], summary
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
        day_dicts = [_day_dict(d) for d in summary["days"]]
        html = render.month_html(month, user.nickname, user.profile, day_dicts, summary)
        try:
            url = await self.html_render(html, {}, return_url=True, umo=event.unified_msg_origin)
        except Exception as exc:  # noqa: BLE001 - t2i 服务未开时回退文本
            logger.warning("排班月历渲染图片失败，回退文本：%s", exc)
            yield event.plain_result(
                render.month_text(month, user.nickname, user.profile, day_dicts, summary)
            )
            return
        yield event.image_result(url)

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
        if opened:
            open_minutes = shift_minutes(
                opened.start, _now_hhmm(), 0
            )
            open_info = {
                "start": opened.start,
                "elapsed_minutes": open_minutes,
            }
        yield event.plain_result(
            render.status_text(
                [_day_dict(d) for d in summary["days"]],
                user.nickname,
                user.profile,
                open_info,
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
                user.profile,
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
                user.profile,
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
                month_label(month), user.nickname, user.profile, summary
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
                user.profile,
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
        if len(args) > 1:
            try:
                overtime = float(args[1].replace("×", "").replace("x", ""))
            except ValueError:
                overtime = user.profile.overtime_rate
            overtime = max(1.0, min(10.0, overtime))
        updated = await self.store.update_profile(
            user.user_key, hourly_rate=rate, overtime_rate=overtime, pay_mode="hourly"
        )
        if updated is None:
            yield event.plain_result("保存失败，请稍后再试。")
            return
        month = f"{date.today():%Y-%m}"
        _, summary = await self._month_stats(updated.user_key, month, updated.profile)
        yield event.plain_result(
            f"✅ 时薪已设为 ¥{rate:g}/h，加班 ×{overtime:g}，计薪方式切到「时薪」。\n"
            f"按 {updated.profile.shift_start}-{updated.profile.shift_end} 每班 "
            f"{human_minutes(updated.profile.daily_minutes)}，日薪约 ¥{updated.profile.daily_minutes / 60 * rate:,.2f}。\n\n"
            + render.summary_block(summary)
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
        updated = await self.store.update_profile(
            user.user_key, monthly_salary=salary, pay_mode="monthly"
        )
        if updated is None:
            yield event.plain_result("保存失败，请稍后再试。")
            return
        month = f"{date.today():%Y-%m}"
        _, summary = await self._month_stats(updated.user_key, month, updated.profile)
        yield event.plain_result(
            f"✅ 月薪已设为 ¥{salary:,.2f}，计薪方式切到「月薪」。\n"
            f"按本月排班计划 {human_minutes(summary['planned_minutes'])} 折算，"
            f"时薪约 ¥{summary['hourly_rate']:g}/h，日薪约 ¥{summary['daily_pay']:,.2f}。\n\n"
            + render.summary_block(summary)
        )

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
                user.profile,
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
        if user.profile.hourly_rate <= 0 and user.profile.monthly_salary <= 0:
            yield event.plain_result(
                "还没设置工资，先发「/工薪 时薪 22.5」或「/工薪 月薪 5000」。"
            )
            return
        planned_hours = summary["planned_minutes"] / 60
        if summary["pay_mode"] == "monthly":
            projected = summary["projected_pay"]
            basis = f"月薪 ¥{user.profile.monthly_salary:,.2f}（折算 ¥{summary['hourly_rate']:g}/h）"
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
            f"推送会话：{channel}\n"
            "只有排班表里当天是「工作日」时才会提醒。\n"
            "调整：/提醒 开｜/提醒 关｜/提醒 上班 30｜/提醒 下班 15"
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

    # ------------------------------------------------------------------ #
    # LLM 函数工具（让机器人 AI 在聊天里自己调用）
    # ------------------------------------------------------------------ #
    @filter.llm_tool("shift_get_schedule")
    async def ai_schedule(self, event: AstrMessageEvent, month: str = "") -> str:
        """查询当前说话人的整月排班表：工作日、休息日、班次、工时与工资概览。

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
        if user.profile.hourly_rate <= 0 and user.profile.monthly_salary <= 0:
            return (
                f"{user.nickname} 还没设置工资，算不出来。"
                "可以让 TA 发「/工薪 时薪 22.5」或「/工薪 月薪 5000」，"
                "也可以在本轮直接说月薪/时薪多少，调用 shift_set_pay 写入。"
            )
        if summary["pay_mode"] == "monthly":
            rate_line = (
                f"月薪 ¥{user.profile.monthly_salary:,.2f}"
                f"（按本月计划 {human_minutes(summary['planned_minutes'])} 折算 ¥{summary['hourly_rate']:g}/h），"
                f"加班倍率 ×{user.profile.overtime_rate:g}\n"
            )
            normal_line = f"月薪基数 ¥{summary['normal_pay']:,.2f}\n"
        else:
            rate_line = f"时薪 ¥{user.profile.hourly_rate:g}/h，加班倍率 ×{user.profile.overtime_rate:g}\n"
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
            f"实发 ¥{summary['total_pay']:,.2f}，全月干满预计 ¥{summary['projected_pay']:,.2f}"
        )

    @filter.llm_tool("shift_set_pay")
    async def ai_set_pay(
        self,
        event: AstrMessageEvent,
        monthly_salary: str = "",
        hourly_rate: str = "",
        pay_mode: str = "",
        overtime_rate: str = "",
    ) -> str:
        """设置当前说话人的工资口径：月薪或时薪。
        只有用户本轮明确说出自己的月薪/时薪数字时才调用；没说数字就不要猜。
        说「月薪 5000」就填 monthly_salary；说「时薪 18」就填 hourly_rate；两个都说了就都填，pay_mode 按用户主要口径填。

        Args:
            monthly_salary(string): 月薪金额，只填数字，如 "5000"；没提到就留空。
            hourly_rate(string): 时薪金额，只填数字，如 "18"；没提到就留空。
            pay_mode(string): 计薪口径，只能填 hourly 或 monthly；留空时按填了哪个自动判断（只填月薪用 monthly，只填时薪用 hourly）。
            overtime_rate(string): 加班倍率，如 "1.5"；留空表示不改。
        """
        if not bool(self.config.get("enable_ai_pay", True)):
            return "管理员没有开启「允许 AI 改工资」，这次没有写库。可以让用户发 /工薪 时薪 或 /工薪 月薪。"

        user = await self._user(event)
        updates: dict[str, object] = {}
        notes: list[str] = []

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
        if mode == "monthly" and "monthly_salary" not in updates and user.profile.monthly_salary <= 0:
            mode = "hourly"
        updates["pay_mode"] = mode

        overtime = _number(overtime_rate) if str(overtime_rate or "").strip() else None
        if overtime is not None:
            updates["overtime_rate"] = max(1.0, min(10.0, overtime))

        updated = await self.store.update_profile(user.user_key, **updates)
        if updated is None:
            return "保存失败，请稍后再试。"

        month = f"{date.today():%Y-%m}"
        _, summary = await self._month_stats(updated.user_key, month, updated.profile)
        basis = (
            f"月薪 ¥{updated.profile.monthly_salary:,.2f}，折算时薪 ¥{summary['hourly_rate']:g}/h"
            if summary["pay_mode"] == "monthly"
            else f"时薪 ¥{updated.profile.hourly_rate:g}/h"
        )
        return (
            f"已设置{'、'.join(notes)}，计薪方式：{'月薪' if summary['pay_mode'] == 'monthly' else '时薪'}\n"
            f"{basis}，日薪约 ¥{summary['daily_pay']:,.2f}\n"
            f"{month_label(month)}实发 ¥{summary['total_pay']:,.2f}，全月预计 ¥{summary['projected_pay']:,.2f}"
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

    # ------------------------------------------------------------------ #
    # WebUI API
    # ------------------------------------------------------------------ #
    async def _snapshot(self, user_key: str, month: str) -> dict:
        record = await self.store.get_user(user_key)
        if record is None:
            return {}
        _, summary = await self._month_stats(user_key, month, record.profile)
        return {
            "active": record.to_dict(),
            "month": month,
            "month_label": month_label(month),
            "days": [_day_dict(day) for day in summary["days"]],
            "summary": _summary_dict(summary),
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
            if "overtime_rate" in payload:
                overtime = float(payload["overtime_rate"])
                if not 1 <= overtime <= 10:
                    return error_response("加班倍率需在 1 ~ 10 之间")
                fields["overtime_rate"] = overtime
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
        except (TypeError, ValueError):
            return error_response("参数格式不正确")

        if fields.get("monthly_salary", 0) > 0 and "pay_mode" not in fields:
            fields["pay_mode"] = "monthly"
        salary_value = fields.get("monthly_salary", record.profile.monthly_salary)
        if fields.get("pay_mode") == "monthly" and float(salary_value or 0) <= 0:
            return error_response("按月薪计薪要先填月薪金额")

        await self.store.update_profile(user_key, **fields)
        month = parse_month(payload.get("month", ""))

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
