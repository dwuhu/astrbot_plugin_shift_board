"""把统计结果渲染成聊天里可读的文本，以及给 t2i 用的月历 HTML。"""

from __future__ import annotations

from datetime import date

from .planner import (
    WEEKDAY_CN,
    DaySlot,
    Profile,
    human_minutes,
    month_bounds,
    month_label,
    shift_type_label,
)

STATUS_ICON = {
    "done": "✅",
    "absent": "❌",
    "planned": "·",
    "missed": "⚠️",
    "rest": "休",
    "overtime": "➕",
}


def _money(value: float) -> str:
    return f"¥{value:,.2f}"


def help_text() -> str:
    return (
        "📖 排班工坊 · 命令一览\n"
        "\n"
        "【排班】\n"
        "/排班 生成 [YYYY-MM]　按当前月休方案重排整月\n"
        "/排班 查看 [YYYY-MM]　看整月排班与统计\n"
        "/排班 本周　　　　　　看本周 7 天\n"
        "/排班 模式 4|6|8　　　切换月休天数并重排\n"
        "/排班 休息 5,14,18,25　指定哪几天休息，其余全上班\n"
        "/排班 休息 自动　　　　取消指定，回到自动排休\n"
        "/排班 罚款 200 迟到　　记一笔罚款，从实发里扣\n"
        "/排班 罚款 清除　　　　清空本月罚款\n"
        "/排班 时间 09:00 18:00 [休60]\n"
        "/排班 偏好 周末|均匀　　休息日是否贴近周末\n"
        "/排班 图 [YYYY-MM]　　把月历渲染成图片\n"
        "\n"
        "【打卡】\n"
        "/打卡 上班　　/打卡 下班　　/打卡 状态\n"
        "/打卡 补 11-20 09:00 18:00　　补录某天\n"
        "/打卡 删 11-20　　　　删除某天打卡\n"
        "\n"
        "【工时】\n"
        "/工时 今日　/工时 本周　/工时 本月 [YYYY-MM]　/工时 明细 [YYYY-MM]\n"
        "\n"
        "【工薪】\n"
        "/工薪 时薪 22.5 [加班倍率1.5]　　按时薪算\n"
        "/工薪 月薪 5000　　　　按月薪算（自动折算时薪）\n"
        "/工薪 本月 [YYYY-MM]　　工资明细\n"
        "/工薪 预测　　　　　　按排班预测全月工资\n"
        "/工薪 日薪　　　　　　一天多少钱\n"
        "\n"
        "【提醒】\n"
        "/提醒 状态　　　　　　看当前提醒设置\n"
        "/提醒 开　/提醒 关　　开关上下班提醒\n"
        "/提醒 上班 30　　　　上班提前几分钟提醒\n"
        "/提醒 下班 15　　　　下班提前几分钟提醒\n"
        "\n"
        "【AI 可以直接调用】\n"
        "月份、今天上不上班、工时、工资都能让 AI 直接查；\n"
        "口头说休息日、班次时长、月薪/时薪、罚款金额，AI 也能直接写进班表；\n"
        "说「11月1号到20号每天8点到20点，5号14号休息」，AI 能批量补历史打卡；\n"
        "打卡默认要自己发指令（管理员可在配置里开放 AI 打卡）。\n"
        "\n"
        "每个 QQ 号的数据独立保存（按 平台+QQ号 分区），换群也不会串数据。"
    )


def header_text(user_nickname: str, profile: Profile) -> str:
    weekend = "周末优先" if profile.prefer_weekend else "均匀排休"
    if profile.pay_mode == "monthly" and profile.monthly_salary > 0:
        pay_text = f"月薪 {_money(profile.monthly_salary)} · 加班 ×{profile.overtime_rate:g}"
    else:
        pay_text = f"时薪 {_money(profile.hourly_rate)}/h · 加班 ×{profile.overtime_rate:g}"
    return (
        f"🗓 排班工坊 · {user_nickname}\n"
        f"月休 {profile.rest_mode} 天 · 班次 {profile.shift_start}-{profile.shift_end}"
        f"（{shift_type_label(profile)}）"
        f" · 休 {profile.break_minutes} 分 · {weekend}\n"
        + pay_text
    )


def summary_block(summary: dict, *, include_money: bool = True) -> str:
    lines = [
        f"📊 工作日出勤 {summary.get('work_attended_days', summary['attended_days'])}"
        f" / 排班 {summary['work_days']} 天"
        f" · 有打卡 {summary['attended_days']} 天"
        f" / 休息 {summary['rest_days']} 天 / 缺勤 {summary['absence_days']} 天",
        f"⏱ 实际 {human_minutes(summary['actual_minutes'])}"
        f" · 计划 {human_minutes(summary['planned_minutes'])}"
        f" · 加班 {human_minutes(summary['overtime_minutes'])}"
        f" · 完成率 {summary['completion']:.1f}%",
    ]
    if include_money:
        if summary["hourly_rate"] <= 0:
            lines.append("💰 还没设置工资，发送 /工薪 时薪 22.5 或 /工薪 月薪 5000 即可开始算")
        else:
            if summary.get("pay_mode") == "monthly":
                head = f"💰 月薪基数 {_money(summary.get('monthly_salary', 0))}"
            else:
                head = f"💰 正常 {_money(summary['normal_pay'])}"
            money_line = (
                f"{head}"
                f" + 加班 {_money(summary['overtime_pay'])}"
                f" - 缺勤 {_money(summary['absence_deduction'])}"
            )
            if summary.get("fine_total"):
                money_line += f" - 罚款 {_money(summary['fine_total'])}"
            money_line += f" = 实发 {_money(summary['total_pay'])}"
            lines.append(money_line)
            lines.append(
                f"🔮 按排班干满全月预计 {_money(summary['projected_pay'])}"
                f" · 日薪 {_money(summary['daily_pay'])}"
            )
    return "\n".join(lines)


def day_line(day: dict) -> str:
    current = date.fromisoformat(day["day"])
    weekday = WEEKDAY_CN[day["weekday"]]
    if day["kind"] == "rest":
        plan = "休息"
    else:
        plan = f"{current.month:02d}/{current.day:02d} {weekday} {day['start']}-{day['end']}"
    icon = STATUS_ICON.get(day["status"], "·")
    actual = human_minutes(day["actual_minutes"]) if day["actual_minutes"] else "—"
    planned = human_minutes(day["planned_minutes"]) if day["planned_minutes"] else "—"
    extra = ""
    if day["overtime_minutes"]:
        extra = f" 加班{human_minutes(day['overtime_minutes'])}"
    if day["kind"] == "rest":
        return f"{icon} {plan}" + (f" · 上班 {actual}{extra}" if day["actual_minutes"] else "")
    return f"{icon} {plan} · 实{actual}/计{planned}{extra}"


def month_text(
    month: str,
    user_nickname: str,
    profile: Profile,
    slot_days: list[dict],
    summary: dict,
) -> str:
    lines = [header_text(user_nickname, profile), f"—— {month_label(month)} ——"]
    for day in slot_days:
        lines.append(day_line(day))
    lines.append("—" * 12)
    lines.append(summary_block(summary))
    return "\n".join(lines)


def week_text(
    start: date,
    end: date,
    user_nickname: str,
    profile: Profile,
    slot_days: list[dict],
    summary: dict,
) -> str:
    lines = [
        header_text(user_nickname, profile),
        f"—— 本周 {start.month}/{start.day} ~ {end.month}/{end.day} ——",
    ]
    for day in slot_days:
        lines.append(day_line(day))
    lines.append("—" * 12)
    lines.append(summary_block(summary))
    return "\n".join(lines)


def remind_text(
    *,
    kind: str,
    nickname: str,
    day: date,
    start: str,
    end: str,
    minutes: int,
    before: int,
    punch_state: str = "none",
    open_at: str = "",
) -> str:
    """上下班提醒文案。kind 取 in / out。"""
    day_text = f"{day.month}月{day.day}日"
    if kind == "in":
        try:
            hour = int(str(start).split(":")[0])
        except (TypeError, ValueError):
            hour = 9
        greeting = "🌙 晚上好" if hour >= 18 or hour < 5 else "🌤 早安"
        return "\n".join(
            [
                f"{greeting}，{nickname}",
                f"今天 {start}-{end}，大概 {human_minutes(minutes)}。",
                f"距离上班还有 {before} 分钟，不用赶，先吃点东西，路上慢一点。",
                "到了发「/打卡 上班」，我帮你把今天记上。",
            ]
        )

    lines = [
        f"🌇 快收工啦，{nickname}",
        f"{day_text} 班次 {start}-{end}，还有 {before} 分钟下班（{end}）。",
    ]
    if punch_state == "open":
        lines.append(f"你 {open_at or start} 的上班卡还在计时，走之前记得「/打卡 下班」。")
    else:
        lines.append("今天还没有完整的打卡记录，记得「/打卡 下班」或补录一次。")
    lines.append("打完下班卡，我马上把今天入账多少、本月累计多少算给你。")
    return "\n".join(lines)


def hours_text(
    title: str,
    user_nickname: str,
    profile: Profile,
    summary: dict,
    *,
    detail_days: list[dict] | None = None,
) -> str:
    lines = [f"⏱ 工时统计 · {user_nickname} · {title}", header_text(user_nickname, profile), ""]
    lines.append(summary_block(summary, include_money=False))
    if detail_days is not None:
        lines.append("")
        lines.append("—— 每日明细 ——")
        for day in detail_days:
            lines.append(day_line(day))
    return "\n".join(lines)


def payroll_text(
    title: str,
    user_nickname: str,
    profile: Profile,
    summary: dict,
    days: list[dict] | None = None,
) -> str:
    if profile.hourly_rate <= 0 and profile.monthly_salary <= 0:
        return (
            f"💤 {user_nickname} 还没设置工资。\n"
            "发送「/工薪 时薪 22.5」按小时算，或「/工薪 月薪 5000」按月薪算。"
        )
    monthly_mode = (
        profile.pay_mode == "monthly" and profile.monthly_salary > 0
    )
    if monthly_mode:
        pay_head = (
            f"月薪 {_money(profile.monthly_salary)} · 加班 ×{profile.overtime_rate:g}"
            f" · 折算时薪 {_money(summary.get('hourly_rate', 0))}/h"
            f" · 日标准 {human_minutes(profile.daily_minutes)}"
        )
        base_line = f"月薪基数 = {_money(summary['normal_pay'])}"
        overtime_line = (
            f"加班工时 {human_minutes(summary['overtime_minutes'])}"
            f" × {_money(summary.get('hourly_rate', 0))}"
            f" × {profile.overtime_rate:g} = {_money(summary['overtime_pay'])}"
        )
    else:
        pay_head = (
            f"时薪 {_money(profile.hourly_rate)}/h · 加班 ×{profile.overtime_rate:g}"
            f" · 日标准 {human_minutes(profile.daily_minutes)}"
        )
        base_line = (
            f"正常工时 {human_minutes(summary['normal_minutes'])} × {_money(profile.hourly_rate)}"
            f" = {_money(summary['normal_pay'])}"
        )
        overtime_line = (
            f"加班工时 {human_minutes(summary['overtime_minutes'])} × {_money(profile.hourly_rate)}"
            f" × {profile.overtime_rate:g} = {_money(summary['overtime_pay'])}"
        )
    lines = [
        f"💰 工薪分析 · {user_nickname} · {title}",
        pay_head,
        "",
        base_line,
        overtime_line,
        f"缺勤扣款 {human_minutes(summary['absence_minutes'])}"
        f" = -{_money(summary['absence_deduction'])}",
        f"罚款合计 = -{_money(summary['fine_total'])}",
        "—" * 12,
        f"实发合计 {_money(summary['total_pay'])}",
        f"全月预计 {_money(summary['projected_pay'])} · 日薪 {_money(summary['daily_pay'])}",
    ]
    if days:
        lines.append("")
        lines.append("—— 每日工薪 ——")
        for day in days:
            if day["kind"] == "rest" and not day["actual_minutes"]:
                continue
            if monthly_mode:
                pay = summary.get("daily_pay", 0)
            else:
                pay = (
                    day["normal_minutes"] / 60 * profile.hourly_rate
                    + day["overtime_minutes"] / 60 * profile.hourly_rate * profile.overtime_rate
                )
            tag = "休班加班" if day["kind"] == "rest" else "出勤"
            lines.append(
                f"{date.fromisoformat(day['day']).strftime('%m/%d')} {tag}"
                f" · 实{human_minutes(day['actual_minutes'])}"
                f" · {_money(pay)}"
            )
    return "\n".join(lines)


def status_text(
    today_days: list[dict],
    user_nickname: str,
    profile: Profile,
    open_punch: dict | None,
) -> str:
    if open_punch:
        return (
            f"🟢 {user_nickname} 正在上班中\n"
            f"上班打卡 {open_punch['start']} · 已计时 "
            f"{open_punch['elapsed_minutes'] // 60}小时{open_punch['elapsed_minutes'] % 60:02d}分"
        )
    lines = [f"⚪ {user_nickname} 当前未在班", header_text(user_nickname, profile), ""]
    for day in today_days:
        lines.append(day_line(day))
    if not today_days:
        lines.append("今天还没有排班记录，发送 /排班 生成 先排出本月班表。")
    return "\n".join(lines)


def punch_result_text(action: str, punch: dict, user_nickname: str) -> str:
    if action == "in":
        return f"🟢 {user_nickname} 上班打卡成功 · {punch['start']}\n下班后发送「/打卡 下班」。"
    return (
        f"🔵 {user_nickname} 下班打卡成功 · {punch['start']}-{punch['end']}"
        f" · 本段 {human_minutes(punch['minutes'])}"
    )


# --------------------------------------------------------------------------- #
# t2i 月历
# --------------------------------------------------------------------------- #
_MONTH_HTML = """
<!doctype html>
<html>
<head><meta charset="utf-8" />
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 32px 36px;
    font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
    background:
      radial-gradient(circle at 12% 8%, rgba(125,211,252,.26), transparent 42%),
      radial-gradient(circle at 88% 6%, rgba(240,171,252,.22), transparent 40%),
      radial-gradient(circle at 70% 96%, rgba(250,204,21,.16), transparent 46%),
      linear-gradient(160deg, #080d1a 0%, #0d1426 52%, #101a2e 100%);
    color: #e8eefc;
  }
  .card {
    background: rgba(255,255,255,.07);
    border: 1px solid rgba(255,255,255,.16);
    border-radius: 26px;
    padding: 26px 28px 30px;
    box-shadow: 0 22px 60px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.22);
    backdrop-filter: blur(18px);
  }
  header { display: flex; justify-content: space-between; align-items: flex-end; }
  .kicker { font-size: 13px; letter-spacing: .28em; color: #7dd3fc; }
  h1 { margin: 6px 0 4px; font-size: 34px; letter-spacing: .02em; }
  .who { font-size: 15px; color: #b7c4de; }
  .mode {
    padding: 8px 16px; border-radius: 999px; font-size: 15px;
    background: linear-gradient(135deg, rgba(125,211,252,.22), rgba(240,171,252,.22));
    border: 1px solid rgba(255,255,255,.24);
  }
  .metrics { display: flex; gap: 12px; margin: 22px 0 20px; }
  .metric {
    flex: 1; padding: 14px 16px; border-radius: 16px;
    background: rgba(255,255,255,.055);
    border: 1px solid rgba(255,255,255,.12);
  }
  .metric span { display: block; font-size: 12px; color: #93a3c0; letter-spacing: .08em; }
  .metric strong { font-size: 26px; font-weight: 700; }
  .metric.money strong { color: #facc15; font-size: 22px; }
  .grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 9px; }
  .wd { text-align: center; font-size: 13px; color: #93a3c0; padding: 4px 0 2px; }
  .cell {
    min-height: 74px; border-radius: 13px; padding: 8px 9px;
    background: rgba(255,255,255,.05);
    border: 1px solid rgba(255,255,255,.1);
  }
  .cell .num { font-size: 17px; font-weight: 700; }
  .cell .tag { margin-top: 4px; font-size: 12px; color: #9fb0cc; }
  .cell.work { background: rgba(56,189,248,.13); border-color: rgba(56,189,248,.34); }
  .cell.rest { background: rgba(148,163,184,.07); border-color: rgba(148,163,184,.2); }
  .cell.done { background: rgba(52,211,153,.16); border-color: rgba(52,211,153,.4); }
  .cell.absent { background: rgba(248,113,113,.16); border-color: rgba(248,113,113,.4); }
  .cell.today { box-shadow: 0 0 0 2px rgba(250,204,21,.8) inset; }
  .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-left: 4px; }
  footer { margin-top: 22px; font-size: 13px; color: #8b9bb8; display: flex; justify-content: space-between; }
</style>
</head>
<body>
  <div class="card">
    <header>
      <div>
        <div class="kicker">SHIFT BOARD / WORKHOURS</div>
        <h1>{{ month_label }}</h1>
        <div class="who">{{ nickname }} · {{ shift }} · 休 {{ break_minutes }} 分</div>
      </div>
      <div class="mode">月休 {{ rest_mode }} 天</div>
    </div>

    <div class="metrics">
      <div class="metric"><span>实际工时</span><strong>{{ actual_hours }}h</strong></div>
      <div class="metric"><span>计划工时</span><strong>{{ planned_hours }}h</strong></div>
      <div class="metric"><span>出勤 / 休息</span><strong>{{ attended_days }} / {{ rest_days }}</strong></div>
      <div class="metric money"><span>实发工资</span><strong>{{ total_pay }}</strong></div>
    </div>

    <div class="grid">
      {% for wd in weekdays %}<div class="wd">{{ wd }}</div>{% endfor %}
      {% for blank in leading %}<div class="cell" style="opacity:.25"></div>{% endfor %}
      {% for day in days %}
      <div class="cell {{ day.cls }}{% if day.today %} today{% endif %}">
        <div class="num">{{ day.day }}</div>
        <div class="tag">{{ day.tag }}</div>
      </div>
      {% endfor %}
    </div>

    <footer>
      <span>时薪 {{ hourly_rate }}/h · 加班 ×{{ overtime_rate }}</span>
      <span>全月预计 {{ projected_pay }}</span>
    </footer>
  </div>
</body>
</html>
"""


def month_html(
    month: str,
    user_nickname: str,
    profile: Profile,
    day_stats: list[dict],
    summary: dict,
) -> str:
    first, last = month_bounds(month)
    leading = ["" for _ in range(first.weekday())]
    days = []
    for day in day_stats:
        status = day["status"]
        if day["kind"] == "rest":
            cls = "rest overtime" if day["actual_minutes"] else "rest"
            tag = f"休息 · 加班{human_minutes(day['actual_minutes'])}" if day["actual_minutes"] else "休息"
        elif status == "done":
            cls = "work done"
            tag = f"{day['start']}-{day['end']} · {human_minutes(day['actual_minutes'])}"
        elif status == "absent":
            cls = "work absent"
            tag = "缺勤"
        else:
            cls = "work"
            tag = f"{day['start']}-{day['end']} · {human_minutes(day['planned_minutes'])}"
        days.append(
            {
                "day": int(day["day"][-2:]),
                "cls": cls,
                "tag": tag,
                "today": day["day"] == date.today().isoformat(),
            }
        )

    return _render_template(
        {
            "month_label": month_label(month),
            "nickname": user_nickname,
            "shift": f"{profile.shift_start}-{profile.shift_end}",
            "break_minutes": profile.break_minutes,
            "rest_mode": profile.rest_mode,
            "actual_hours": f"{summary['actual_hours']:.1f}",
            "planned_hours": f"{summary['planned_hours']:.1f}",
            "attended_days": summary["attended_days"],
            "rest_days": summary["rest_days"],
            "total_pay": f"¥{summary['total_pay']:,.0f}",
            "weekdays": list(WEEKDAY_CN),
            "leading": leading,
            "days": days,
            "hourly_rate": f"¥{profile.hourly_rate:g}",
            "overtime_rate": f"{profile.overtime_rate:g}",
            "projected_pay": f"¥{summary['projected_pay']:,.0f}",
        }
    )


def _render_template(context: dict) -> str:
    """尽量用 Jinja2 渲染；万一环境里没有 Jinja2 就退回极简替换。"""
    try:
        from jinja2 import Template

        return Template(_MONTH_HTML).render(**context)
    except Exception:  # noqa: BLE001 - 模板渲染失败不能让指令崩掉
        html = _MONTH_HTML
        for key, value in context.items():
            if isinstance(value, (str, int, float)):
                html = html.replace("{{ " + key + " }}", str(value))
        return html
