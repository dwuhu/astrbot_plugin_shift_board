"""按用户隔离的 SQLite 存储层。

每个 QQ 用户一条 profiles 记录，排班表与打卡记录都以 user_key 为分区键，
查询时永远带上 user_key，天然做到「各人数据互不干扰」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiosqlite

from .planner import DaySlot, Profile, Punch

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    user_key             TEXT PRIMARY KEY,
    uid                  TEXT NOT NULL DEFAULT '',
    platform             TEXT NOT NULL DEFAULT '',
    nickname             TEXT NOT NULL DEFAULT '',
    rest_mode            INTEGER NOT NULL DEFAULT 4,
    shift_start          TEXT NOT NULL DEFAULT '09:00',
    shift_end            TEXT NOT NULL DEFAULT '18:00',
    break_minutes        INTEGER NOT NULL DEFAULT 60,
    prefer_weekend       INTEGER NOT NULL DEFAULT 1,
    hourly_rate          REAL NOT NULL DEFAULT 0,
    monthly_salary       REAL NOT NULL DEFAULT 0,
    pay_mode             TEXT NOT NULL DEFAULT 'hourly',
    overtime_rate        REAL NOT NULL DEFAULT 1.5,
    absence_deduction    INTEGER NOT NULL DEFAULT 0,
    rest_day_as_overtime INTEGER NOT NULL DEFAULT 1,
    umo                  TEXT NOT NULL DEFAULT '',
    remind_enabled       INTEGER NOT NULL DEFAULT 1,
    remind_before_start  INTEGER NOT NULL DEFAULT 30,
    remind_before_end    INTEGER NOT NULL DEFAULT 15,
    created_at           TEXT NOT NULL DEFAULT '',
    updated_at           TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS schedules (
    user_key         TEXT NOT NULL,
    month            TEXT NOT NULL,
    day              TEXT NOT NULL,
    kind             TEXT NOT NULL,
    start            TEXT NOT NULL DEFAULT '',
    end              TEXT NOT NULL DEFAULT '',
    planned_minutes  INTEGER NOT NULL DEFAULT 0,
    weekday          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_key, day)
);

CREATE TABLE IF NOT EXISTS punches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key    TEXT NOT NULL,
    day         TEXT NOT NULL,
    start       TEXT NOT NULL,
    end         TEXT,
    minutes     INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_punches_user_day ON punches (user_key, day);
CREATE INDEX IF NOT EXISTS idx_schedules_user_month ON schedules (user_key, month);

CREATE TABLE IF NOT EXISTS reminders (
    user_key  TEXT NOT NULL,
    day       TEXT NOT NULL,
    kind      TEXT NOT NULL,
    sent_at   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_key, day, kind)
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS fines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key   TEXT NOT NULL,
    month      TEXT NOT NULL,
    amount     REAL NOT NULL DEFAULT 0,
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_fines_user_month ON fines(user_key, month);

CREATE TABLE IF NOT EXISTS month_rests (
    user_key   TEXT NOT NULL,
    month      TEXT NOT NULL,
    days       TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_key, month)
);
"""

# 老版本数据库补齐新增列（升级时不丢数据）
_PROFILE_EXTRA_COLUMNS = {
    "umo": "TEXT NOT NULL DEFAULT ''",
    "remind_enabled": "INTEGER NOT NULL DEFAULT 1",
    "remind_before_start": "INTEGER NOT NULL DEFAULT 30",
    "remind_before_end": "INTEGER NOT NULL DEFAULT 15",
    "monthly_salary": "REAL NOT NULL DEFAULT 0",
    "pay_mode": "TEXT NOT NULL DEFAULT 'hourly'",
}

_ALLOWED_FIELDS = {
    "nickname",
    "rest_mode",
    "shift_start",
    "shift_end",
    "break_minutes",
    "prefer_weekend",
    "hourly_rate",
    "monthly_salary",
    "pay_mode",
    "overtime_rate",
    "absence_deduction",
    "rest_day_as_overtime",
    "umo",
    "remind_enabled",
    "remind_before_start",
    "remind_before_end",
}


@dataclass(slots=True)
class UserRecord:
    user_key: str
    uid: str
    platform: str
    nickname: str
    profile: Profile
    created_at: str = ""
    updated_at: str = ""
    umo: str = ""
    remind_enabled: bool = True
    remind_before_start: int = 30
    remind_before_end: int = 15

    def to_dict(self) -> dict:
        return {
            "user_key": self.user_key,
            "uid": self.uid,
            "platform": self.platform,
            "nickname": self.nickname,
            "rest_mode": self.profile.rest_mode,
            "shift_start": self.profile.shift_start,
            "shift_end": self.profile.shift_end,
            "break_minutes": self.profile.break_minutes,
            "prefer_weekend": self.profile.prefer_weekend,
            "hourly_rate": self.profile.hourly_rate,
            "monthly_salary": self.profile.monthly_salary,
            "pay_mode": self.profile.pay_mode,
            "overtime_rate": self.profile.overtime_rate,
            "absence_deduction": self.profile.absence_deduction,
            "rest_day_as_overtime": self.profile.rest_day_as_overtime,
            "daily_minutes": self.profile.daily_minutes,
            "umo": self.umo,
            "remind_enabled": self.remind_enabled,
            "remind_before_start": self.remind_before_start,
            "remind_before_end": self.remind_before_end,
            "updated_at": self.updated_at,
        }


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ShiftStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._ready = False

    async def init(self) -> None:
        if self._ready:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.executescript(SCHEMA)
            cursor = await conn.execute("PRAGMA table_info(profiles)")
            existing = {row[1] for row in await cursor.fetchall()}
            for column, definition in _PROFILE_EXTRA_COLUMNS.items():
                if column not in existing:
                    await conn.execute(
                        f"ALTER TABLE profiles ADD COLUMN {column} {definition}"
                    )
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.commit()
        self._ready = True

    async def _connect(self) -> aiosqlite.Connection:
        await self.init()
        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row
        return conn

    # ------------------------------------------------------------------ #
    # 用户资料
    # ------------------------------------------------------------------ #
    async def ensure_user(
        self,
        user_key: str,
        *,
        uid: str = "",
        platform: str = "",
        nickname: str = "",
        defaults: Profile | None = None,
        umo: str = "",
        remind_defaults: dict | None = None,
    ) -> UserRecord:
        defaults = (defaults or Profile()).normalized()
        remind_defaults = remind_defaults or {}
        remind_enabled = int(bool(remind_defaults.get("enabled", True)))
        remind_before_start = max(
            0, min(240, int(remind_defaults.get("before_start", 30)))
        )
        remind_before_end = max(
            0, min(240, int(remind_defaults.get("before_end", 15)))
        )
        now = _now()
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO profiles (
                    user_key, uid, platform, nickname, rest_mode, shift_start, shift_end,
                    break_minutes, prefer_weekend, hourly_rate, monthly_salary,
                    pay_mode, overtime_rate,
                    absence_deduction, rest_day_as_overtime, umo,
                    remind_enabled, remind_before_start, remind_before_end,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_key) DO UPDATE SET
                    uid = excluded.uid,
                    platform = excluded.platform,
                    nickname = CASE WHEN excluded.nickname <> '' THEN excluded.nickname ELSE profiles.nickname END,
                    umo = CASE WHEN excluded.umo <> '' THEN excluded.umo ELSE profiles.umo END,
                    updated_at = excluded.updated_at
                """,
                (
                    user_key,
                    uid,
                    platform,
                    nickname,
                    defaults.rest_mode,
                    defaults.shift_start,
                    defaults.shift_end,
                    defaults.break_minutes,
                    int(defaults.prefer_weekend),
                    defaults.hourly_rate,
                    defaults.monthly_salary,
                    defaults.pay_mode,
                    defaults.overtime_rate,
                    int(defaults.absence_deduction),
                    int(defaults.rest_day_as_overtime),
                    umo,
                    remind_enabled,
                    remind_before_start,
                    remind_before_end,
                    now,
                    now,
                ),
            )
            await conn.commit()
        finally:
            await conn.close()
        record = await self.get_user(user_key)
        return record or UserRecord(
            user_key, uid, platform, nickname, defaults, now, now, umo, True, 30, 15
        )

    async def get_user(self, user_key: str) -> UserRecord | None:
        conn = await self._connect()
        try:
            cursor = await conn.execute(
                "SELECT * FROM profiles WHERE user_key = ?", (user_key,)
            )
            row = await cursor.fetchone()
        finally:
            await conn.close()
        return self._row_to_user(row) if row else None

    async def list_users(self, limit: int = 300) -> list[UserRecord]:
        conn = await self._connect()
        try:
            cursor = await conn.execute(
                "SELECT * FROM profiles ORDER BY updated_at DESC, nickname LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
        finally:
            await conn.close()
        return [self._row_to_user(row) for row in rows]

    async def update_profile(self, user_key: str, **fields) -> UserRecord | None:
        payload = {k: v for k, v in fields.items() if k in _ALLOWED_FIELDS}
        if not payload:
            return await self.get_user(user_key)
        if "prefer_weekend" in payload:
            payload["prefer_weekend"] = int(bool(payload["prefer_weekend"]))
        if "absence_deduction" in payload:
            payload["absence_deduction"] = int(bool(payload["absence_deduction"]))
        if "rest_day_as_overtime" in payload:
            payload["rest_day_as_overtime"] = int(bool(payload["rest_day_as_overtime"]))
        if "rest_mode" in payload:
            payload["rest_mode"] = int(payload["rest_mode"])
        if "break_minutes" in payload:
            payload["break_minutes"] = int(payload["break_minutes"])
        if "remind_enabled" in payload:
            payload["remind_enabled"] = int(bool(payload["remind_enabled"]))
        if "remind_before_start" in payload:
            payload["remind_before_start"] = max(
                0, min(240, int(payload["remind_before_start"]))
            )
        if "remind_before_end" in payload:
            payload["remind_before_end"] = max(
                0, min(240, int(payload["remind_before_end"]))
            )

        payload["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in payload)
        conn = await self._connect()
        try:
            await conn.execute(
                f"UPDATE profiles SET {assignments} WHERE user_key = ?",
                (*payload.values(), user_key),
            )
            await conn.commit()
        finally:
            await conn.close()
        return await self.get_user(user_key)

    def _row_to_user(self, row: aiosqlite.Row) -> UserRecord:
        profile = Profile(
            rest_mode=int(row["rest_mode"] or 4),
            shift_start=row["shift_start"] or "09:00",
            shift_end=row["shift_end"] or "18:00",
            break_minutes=int(row["break_minutes"] or 0),
            prefer_weekend=bool(row["prefer_weekend"]),
            hourly_rate=float(row["hourly_rate"] or 0),
            monthly_salary=float(row["monthly_salary"] or 0),
            pay_mode=row["pay_mode"] or "hourly",
            overtime_rate=float(row["overtime_rate"] or 1.5),
            absence_deduction=bool(row["absence_deduction"]),
            rest_day_as_overtime=bool(row["rest_day_as_overtime"]),
        )
        return UserRecord(
            user_key=row["user_key"],
            uid=row["uid"] or "",
            platform=row["platform"] or "",
            nickname=row["nickname"] or row["user_key"],
            profile=profile,
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            umo=row["umo"] or "",
            remind_enabled=bool(row["remind_enabled"]),
            remind_before_start=int(row["remind_before_start"] or 0),
            remind_before_end=int(row["remind_before_end"] or 0),
        )

    # ------------------------------------------------------------------ #
    # 排班
    # ------------------------------------------------------------------ #
    async def has_schedule(self, user_key: str, month: str) -> bool:
        conn = await self._connect()
        try:
            cursor = await conn.execute(
                "SELECT 1 FROM schedules WHERE user_key = ? AND month = ? LIMIT 1",
                (user_key, month),
            )
            return await cursor.fetchone() is not None
        finally:
            await conn.close()

    async def save_schedule(self, user_key: str, month: str, slots: list[DaySlot]) -> None:
        conn = await self._connect()
        try:
            await conn.execute(
                "DELETE FROM schedules WHERE user_key = ? AND month = ?",
                (user_key, month),
            )
            await conn.executemany(
                """
                INSERT INTO schedules
                    (user_key, month, day, kind, start, end, planned_minutes, weekday)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        user_key,
                        month,
                        slot.day,
                        slot.kind,
                        slot.start,
                        slot.end,
                        slot.planned_minutes,
                        slot.weekday,
                    )
                    for slot in slots
                ],
            )
            await conn.commit()
        finally:
            await conn.close()

    async def get_schedule(self, user_key: str, month: str) -> list[DaySlot]:
        conn = await self._connect()
        try:
            cursor = await conn.execute(
                """
                SELECT day, kind, start, end, planned_minutes, weekday
                FROM schedules WHERE user_key = ? AND month = ?
                ORDER BY day
                """,
                (user_key, month),
            )
            rows = await cursor.fetchall()
        finally:
            await conn.close()
        return [
            DaySlot(
                day=row["day"],
                kind=row["kind"],
                start=row["start"],
                end=row["end"],
                planned_minutes=int(row["planned_minutes"] or 0),
                weekday=int(row["weekday"] or 0),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    # 打卡
    # ------------------------------------------------------------------ #
    async def open_punch(self, user_key: str, day: str, start: str) -> Punch:
        conn = await self._connect()
        try:
            cursor = await conn.execute(
                """
                INSERT INTO punches (user_key, day, start, end, minutes, note, created_at)
                VALUES (?, ?, ?, NULL, 0, ?, ?)
                """,
                (user_key, day, start, "上班打卡", _now()),
            )
            await conn.commit()
            punch_id = cursor.lastrowid
        finally:
            await conn.close()
        return Punch(day=day, start=start, end=None, minutes=0, note="上班打卡", punch_id=punch_id)

    async def close_open_punch(
        self, user_key: str, day: str, end: str, minutes: int
    ) -> Punch | None:
        conn = await self._connect()
        try:
            cursor = await conn.execute(
                """
                SELECT id, start FROM punches
                WHERE user_key = ? AND day = ? AND end IS NULL
                ORDER BY id DESC LIMIT 1
                """,
                (user_key, day),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            await conn.execute(
                "UPDATE punches SET end = ?, minutes = ?, note = ? WHERE id = ?",
                (end, max(0, int(minutes)), "上班-下班", row["id"]),
            )
            await conn.commit()
            return Punch(
                day=day,
                start=row["start"],
                end=end,
                minutes=max(0, int(minutes)),
                note="上班-下班",
                punch_id=row["id"],
            )
        finally:
            await conn.close()

    async def add_punch(
        self, user_key: str, day: str, start: str, end: str, minutes: int, note: str = "补录"
    ) -> Punch:
        conn = await self._connect()
        try:
            cursor = await conn.execute(
                """
                INSERT INTO punches (user_key, day, start, end, minutes, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_key, day, start, end, max(0, int(minutes)), note, _now()),
            )
            await conn.commit()
            punch_id = cursor.lastrowid
        finally:
            await conn.close()
        return Punch(day, start, end, max(0, int(minutes)), note, punch_id)

    async def list_punches(self, user_key: str, start_day: str, end_day: str) -> list[Punch]:
        conn = await self._connect()
        try:
            cursor = await conn.execute(
                """
                SELECT id, day, start, end, minutes, note FROM punches
                WHERE user_key = ? AND day >= ? AND day <= ?
                ORDER BY day, id
                """,
                (user_key, start_day, end_day),
            )
            rows = await cursor.fetchall()
        finally:
            await conn.close()
        return [
            Punch(
                day=row["day"],
                start=row["start"],
                end=row["end"],
                minutes=int(row["minutes"] or 0),
                note=row["note"] or "",
                punch_id=row["id"],
            )
            for row in rows
        ]

    async def delete_day_punches(self, user_key: str, day: str) -> int:
        conn = await self._connect()
        try:
            cursor = await conn.execute(
                "DELETE FROM punches WHERE user_key = ? AND day = ?", (user_key, day)
            )
            await conn.commit()
            return cursor.rowcount or 0
        finally:
            await conn.close()

    async def delete_punch(self, user_key: str, punch_id: int) -> bool:
        conn = await self._connect()
        try:
            cursor = await conn.execute(
                "DELETE FROM punches WHERE user_key = ? AND id = ?", (user_key, punch_id)
            )
            await conn.commit()
            return bool(cursor.rowcount)
        finally:
            await conn.close()

    # ------------------------------------------------------------------ #
    # 上下班提醒
    # ------------------------------------------------------------------ #
    async def mark_reminded(self, user_key: str, day: str, kind: str) -> bool:
        """原子占位：返回 True 表示这条提醒今天还没发过。"""
        conn = await self._connect()
        try:
            cursor = await conn.execute(
                """
                INSERT OR IGNORE INTO reminders (user_key, day, kind, sent_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_key, day, kind, _now()),
            )
            await conn.commit()
            return bool(cursor.rowcount)
        finally:
            await conn.close()

    async def prune_reminders(self, before_day: str) -> int:
        conn = await self._connect()
        try:
            cursor = await conn.execute(
                "DELETE FROM reminders WHERE day < ?", (before_day,)
            )
            await conn.commit()
            return cursor.rowcount or 0
        finally:
            await conn.close()

    async def set_umo(self, user_key: str, umo: str) -> None:
        if not umo:
            return
        await self.update_profile(user_key, umo=umo)

    # ------------------------------------------------------------------ #
    # 全局设置（背景、外观等，与具体员工无关）
    # ------------------------------------------------------------------ #
    async def get_setting(self, key: str, default: str = "") -> str:
        conn = await self._connect()
        try:
            cursor = await conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            )
            row = await cursor.fetchone()
        finally:
            await conn.close()
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, _now()),
            )
            await conn.commit()
        finally:
            await conn.close()

    # ------------------------------------------------------------------ #
    # 月度指定休息日（用户口头说「5、14、18、25 休息」时用）
    # ------------------------------------------------------------------ #
    async def set_month_rests(
        self, user_key: str, month: str, days: list[int]
    ) -> None:
        payload = ",".join(str(int(day)) for day in sorted(set(days)))
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO month_rests (user_key, month, days, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(user_key, month) DO UPDATE
                SET days = excluded.days, updated_at = excluded.updated_at
                """,
                (user_key, month, payload, _now()),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def get_month_rests(self, user_key: str, month: str) -> list[int] | None:
        """返回本月手工指定的休息日；返回 None 表示没指定，继续用月休算法。"""
        conn = await self._connect()
        try:
            cursor = await conn.execute(
                "SELECT days FROM month_rests WHERE user_key = ? AND month = ?",
                (user_key, month),
            )
            row = await cursor.fetchone()
        finally:
            await conn.close()
        if row is None:
            return None
        days = [int(part) for part in str(row["days"] or "").split(",") if part.strip().isdigit()]
        return sorted(set(days))

    async def clear_month_rests(self, user_key: str, month: str) -> None:
        conn = await self._connect()
        try:
            await conn.execute(
                "DELETE FROM month_rests WHERE user_key = ? AND month = ?",
                (user_key, month),
            )
            await conn.commit()
        finally:
            await conn.close()

    # ------------------------------------------------------------------ #
    # 罚款（按人 + 按月累计，直接从实发工资里扣）
    # ------------------------------------------------------------------ #
    async def add_fine(
        self, user_key: str, month: str, amount: float, reason: str = ""
    ) -> dict:
        conn = await self._connect()
        try:
            cursor = await conn.execute(
                "INSERT INTO fines (user_key, month, amount, reason, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (user_key, month, float(amount), str(reason or ""), _now()),
            )
            await conn.commit()
            return {
                "id": cursor.lastrowid,
                "month": month,
                "amount": round(float(amount), 2),
                "reason": str(reason or ""),
            }
        finally:
            await conn.close()

    async def list_fines(self, user_key: str, month: str) -> list[dict]:
        conn = await self._connect()
        try:
            cursor = await conn.execute(
                "SELECT id, month, amount, reason, created_at FROM fines"
                " WHERE user_key = ? AND month = ? ORDER BY id",
                (user_key, month),
            )
            rows = await cursor.fetchall()
        finally:
            await conn.close()
        return [
            {
                "id": row["id"],
                "month": row["month"],
                "amount": round(float(row["amount"] or 0), 2),
                "reason": row["reason"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def fine_total(self, user_key: str, month: str) -> float:
        conn = await self._connect()
        try:
            cursor = await conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS total FROM fines"
                " WHERE user_key = ? AND month = ?",
                (user_key, month),
            )
            row = await cursor.fetchone()
        finally:
            await conn.close()
        return round(float(row["total"] or 0), 2) if row else 0.0

    async def clear_fines(self, user_key: str, month: str) -> int:
        conn = await self._connect()
        try:
            cursor = await conn.execute(
                "DELETE FROM fines WHERE user_key = ? AND month = ?",
                (user_key, month),
            )
            await conn.commit()
            return int(cursor.rowcount or 0)
        finally:
            await conn.close()

    async def delete_fine(self, user_key: str, fine_id: int) -> bool:
        conn = await self._connect()
        try:
            cursor = await conn.execute(
                "DELETE FROM fines WHERE user_key = ? AND id = ?",
                (user_key, int(fine_id)),
            )
            await conn.commit()
            return bool(cursor.rowcount)
        finally:
            await conn.close()

    # ------------------------------------------------------------------ #
    # 整库备份 / 导入
    # ------------------------------------------------------------------ #
    async def export_all(self) -> dict:
        """把七张表导成一份 JSON，用于换机器、重装后恢复。"""
        conn = await self._connect()
        try:
            tables: dict[str, list[dict]] = {}
            for table in _DUMP_TABLES:
                cursor = await conn.execute(f"SELECT * FROM {table}")
                tables[table] = [dict(row) for row in await cursor.fetchall()]
        finally:
            await conn.close()
        return {
            "plugin": "astrbot_plugin_shift_board",
            "format": 1,
            "exported_at": _now(),
            "tables": tables,
        }

    async def import_all(self, payload: object, replace: bool = False) -> dict:
        """导入 export_all() 生成的备份。

        replace=True 会先清空现有数据（危险，界面上要求二次确认）；
        否则按主键合并：已有的人/班表/设置被覆盖，打卡只补不重复。
        """
        if not isinstance(payload, dict):
            raise ValueError("备份内容为空或格式不对")
        tables = payload.get("tables")
        if not isinstance(tables, dict):
            raise ValueError("这不是排班工坊的备份文件（缺少 tables）")

        conn = await self._connect()
        result: dict[str, int] = {}
        try:
            for table in _DUMP_TABLES:
                raw_rows = tables.get(table, [])
                if raw_rows is None:
                    raw_rows = []
                if not isinstance(raw_rows, list):
                    raise ValueError(f"{table} 的内容不是列表")
                if len(raw_rows) > 50000:
                    raise ValueError(f"{table} 行数过多，拒绝导入")
                cursor = await conn.execute(f"PRAGMA table_info({table})")
                columns = [row[1] for row in await cursor.fetchall()]
                if replace:
                    await conn.execute(f"DELETE FROM {table}")
                inserted = 0
                for raw in raw_rows:
                    if not isinstance(raw, dict):
                        continue
                    row = {key: raw[key] for key in columns if key in raw}
                    if not row:
                        continue
                    if table in {"punches", "fines"}:
                        row.pop("id", None)
                        if not replace and await _row_exists(conn, table, row):
                            continue
                    placeholders = ", ".join("?" for _ in row)
                    await conn.execute(
                        f"INSERT OR REPLACE INTO {table} ({', '.join(row.keys())})"
                        f" VALUES ({placeholders})",
                        tuple(row.values()),
                    )
                    inserted += 1
                result[table] = inserted
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            await conn.close()
        return result


_DUMP_TABLES = (
    "profiles",
    "schedules",
    "punches",
    "reminders",
    "fines",
    "month_rests",
    "settings",
)

_PUNCH_DEDUPE_COLUMNS = ("user_key", "day", "start", "end", "minutes")
_FINE_DEDUPE_COLUMNS = ("user_key", "month", "amount", "reason")


async def _row_exists(conn: aiosqlite.Connection, table: str, row: dict) -> bool:
    columns = _PUNCH_DEDUPE_COLUMNS if table == "punches" else _FINE_DEDUPE_COLUMNS
    if any(column not in row for column in columns):
        return False
    where = " AND ".join(f"{column} = ?" for column in columns)
    cursor = await conn.execute(
        f"SELECT 1 FROM {table} WHERE {where} LIMIT 1",
        tuple(row[column] for column in columns),
    )
    return await cursor.fetchone() is not None

