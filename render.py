"""把统计结果渲染成聊天里可读的文本，以及给 t2i 用的月历 HTML。"""

from __future__ import annotations

from datetime import date
from html import escape

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
    "leave": "假",
    "prejob": "未",
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
        "/排班 请假 10月1号到10月21号　标未上班/请假，不计薪\n"
        "/排班 请假 恢复 10月1号到10月21号　恢复工作日\n"
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
        "/工薪 时薪 22.5 [加班倍率] [YYYY-MM]　按时薪算，可只改某个月\n"
        "/工薪 月薪 5000 [YYYY-MM]　　按月薪算，只改指定月份\n"
        "/工薪 阶段 实习|正式 [YYYY-MM]　按月标记实习 / 正式\n"
        "/工薪 年收入 [YYYY]　　按实际上班天数累计，不是预测\n"
        "/工薪 加班 1.5 [标准8]　　改加班倍率 / 每日标准工时\n"
        "/工薪 本月 [YYYY-MM]　　工资明细（含固定扣款）\n"
        "/工薪 预测　　　　　　按排班预测全月到手\n"
        "/工薪 日薪　　　　　　一天多少钱\n"
        "\n"
        "【每月扣款】\n"
        "/扣款 添加 房租 1500 每月　　标签自己起，每月自动扣\n"
        "/扣款 添加 水电费 120 本月　　只扣这个月\n"
        "/扣款 查看 [YYYY-MM]　　扣款明细 + 扣款后到手\n"
        "/扣款 删除 3 或 房租　　按编号或标签删\n"
        "\n"
        "【提醒】\n"
        "/提醒 状态　　　　　　看当前提醒设置\n"
        "/提醒 开　/提醒 关　　开关上下班提醒\n"
        "/提醒 上班 30　　　　上班提前几分钟提醒\n"
        "/提醒 下班 15　　　　下班提前几分钟提醒\n"
        "/提醒 加班 开/关　　　超过标准工时提醒\n"
        "/提醒 加班 8　　　　　改每日标准工时\n"
        "/提醒 休息 开/关　　　今天上班、明天排休才提醒\n"
        "/提醒 自定义 上班 早安 {nickname}　自定义文案\n"
        "/提醒 自定义 下班 收工啦 {nickname}\n"
        "/提醒 城市 宜宾　　　设置城市，AI 联网查天气\n"
        "/提醒 天气 开/关　　 单独开关天气提醒\n"
        "休息日提醒只在前一天上班、后一天排休时出现；{tomorrow} 可放进自定义文案。\n"
        "\n"
        "【AI 可以直接调用】\n"
        "月份、今天上不上班、工时、工资、提醒设置都能让 AI 直接查；\n"
        "说「排班图」或「把班表发成图片」，AI 会单独发一张整月排班月历；\n"
        "口头说休息日、班次时长、月薪/时薪、罚款金额，AI 也能直接写进班表；\n"
        "说「每月房租1500、水电120、餐费600」，AI 会按你起的标签加扣款；\n"
        "说「加班1.5倍、每天超过8小时提醒我」，AI 会改加班规则；\n"
        "提醒文案、天气城市、删除某天打卡也可以让 AI 代办；\n"
        "说「11月1号到20号每天8点到20点，5号14号休息」，AI 能批量补历史打卡。\n"
        "\n"
        "每个 QQ 号的数据独立保存（按 平台+QQ号 分区），换群也不会串数据。"
    )


# --------------------------------------------------------------------------- #
# t2i 帮助卡片
# --------------------------------------------------------------------------- #
_HELP_GROUPS = [
    (
        "🗓",
        "排班",
        "SHIFT",
        "#38bdf8",
        [
            ("/排班 帮助", "就是这张图文手册"),
            ("/排班 生成 [YYYY-MM]", "按当前方案重排整月"),
            ("/排班 查看 [YYYY-MM]", "整月班表 + 工时 + 工资"),
            ("/排班 本周", "看本周 7 天"),
            ("/排班 休息 5,14,18,25", "点名休息日，其余都上班"),
            ("/排班 请假 10月1号到10月21号", "未上班 / 请假，不计薪"),
            ("/排班 请假 恢复 <日期>", "恢复为正常工作日"),
            ("/排班 时间 09:00 18:00 60", "改班次和每班扣休"),
            ("/排班 罚款 200 迟到", "记罚款，从实发里扣"),
            ("/排班 图 [YYYY-MM]", "把月历渲染成图片"),
        ],
    ),
    (
        "⏱",
        "打卡与工时",
        "CLOCK",
        "#34d399",
        [
            ("/打卡 上班", "开始计时"),
            ("/打卡 下班", "结束并结算今天入账"),
            ("/打卡 状态", "在不在班、现在几点、今天上了多久、距下班还剩多久"),
            ("/打卡 补 11-20 09:00 18:00", "补录某天"),
            ("/打卡 删 11-20", "清空某天打卡"),
            ("/工时 今日 / 本周", "快速看工时"),
            ("/工时 本月 [YYYY-MM]", "整月汇总"),
            ("/工时 明细 [YYYY-MM]", "每天一行明细"),
        ],
    ),
    (
        "💰",
        "工薪",
        "PAYROLL",
        "#facc15",
        [
            ("/工薪 时薪 22.5 1.5", "时薪 + 加班倍率"),
            ("/工薪 时薪 22.5 2026-10", "只改这个月的时薪"),
            ("/工薪 月薪 5000", "月薪制，自动折算时薪"),
            ("/工薪 月薪 5000 2026-10", "只改这个月，历史不覆盖"),
            ("/工薪 阶段 实习 2026-10", "按月标记实习 / 正式"),
            ("/工薪 年收入 [2026]", "按实际上班天数累计"),
            ("/工薪 加班 1.5 8", "加班倍率 + 每日标准工时"),
            ("/工薪 本月 [YYYY-MM]", "正常 + 加班 − 缺勤 − 罚款 − 扣款"),
            ("/工薪 预测", "干满全月预计到手"),
            ("/工薪 日薪", "一天多少钱"),
        ],
    ),
    (
        "🧾",
        "每月扣款",
        "DEDUCTIONS",
        "#fb923c",
        [
            ("/扣款", "看本月扣款与扣款后到手"),
            ("/扣款 添加 房租 1500 每月", "标签自定义，每月自动扣"),
            ("/扣款 添加 水电费 120 本月", "只扣这一个月"),
            ("/扣款 查看 [YYYY-MM]", "明细 + 合计 + 到手"),
            ("/扣款 删除 3 / 房租", "按编号或标签删除"),
            ("后台费用环形图", "社保、水电、餐费各占多少"),
        ],
    ),
    (
        "🔔",
        "提醒与天气",
        "REMINDER",
        "#f472b6",
        [
            ("/提醒 状态", "开关、提前分钟、文案、城市"),
            ("/提醒 上班 30 / 下班 15", "改提前提醒分钟"),
            ("/提醒 加班 开 / 关", "超过标准工时提醒一次"),
            ("/提醒 加班 8", "改每日标准工时"),
            ("/提醒 休息 开 / 关", "今天上班、明天排休才提醒"),
            ("/提醒 城市 宜宾", "设置天气城市并开启天气"),
            ("/提醒 天气 开 / 关", "单独开关天气提醒"),
            ("{tomorrow}", "自定义文案用，休息日预告"),
        ],
    ),
    (
        "🏦",
        "发薪与考勤图",
        "PAYDAY",
        "#22d3ee",
        [
            ("/今日考勤", "今日工时、时薪、今天已获得多少钱"),
            ("/发薪 查看", "应发、已到账、未发合计"),
            ("/发薪 月底", "当月工资当月最后一天发"),
            ("/发薪 次月 15", "压一月，次月 15 号发"),
            ("/发薪 提醒 开 / 17:30", "发薪日到点提醒到账"),
            ("/发薪 到账 2026-10 5000", "登记某月工资已到账"),
            ("/发薪 撤销 2026-10", "撤销到账登记"),
        ],
    ),
]

_AI_TOOL_NAMES = [
    "shift_get_schedule",
    "shift_schedule_image",
    "shift_get_today",
    "shift_get_hours",
    "shift_get_pay",
    "shift_get_annual_income",
    "shift_get_profile",
    "shift_set_pay",
    "shift_set_plan",
    "shift_set_reminder",
    "shift_add_fine",
    "shift_punch",
    "shift_delete_punch",
    "shift_bulk_punch",
    "shift_get_deductions",
    "shift_add_deduction",
    "shift_delete_deduction",
    "shift_set_overtime",
    "shift_today_attendance",
    "shift_get_payroll",
    "shift_set_payroll",
    "shift_mark_salary_paid",
]

_HELP_HTML = """
<!doctype html>
<html>
<head><meta charset="utf-8" />
<style>
  * { box-sizing: border-box; }
  body {
    max-width: 960px; margin: 0 auto; padding: 26px 20px 30px;
    font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans SC", sans-serif;
    color: #e8eefc;
    background:
      radial-gradient(circle at 8% 4%, rgba(56,189,248,.28), transparent 38%),
      radial-gradient(circle at 92% 2%, rgba(244,114,182,.22), transparent 36%),
      radial-gradient(circle at 72% 98%, rgba(250,204,21,.16), transparent 42%),
      linear-gradient(160deg, #070b16 0%, #0c1324 52%, #111a2e 100%);
  }
  .shell {
    position: relative; overflow: hidden;
    border-radius: 28px; padding: 28px 30px 24px;
    background: rgba(255,255,255,.065);
    border: 1px solid rgba(255,255,255,.16);
    box-shadow: 0 26px 70px rgba(0,0,0,.5), inset 0 1px 0 rgba(255,255,255,.24);
  }
  .shell::before {
    content: ""; position: absolute; inset: -40% -20% auto;
    height: 260px; background: linear-gradient(120deg, rgba(56,189,248,.2), rgba(167,139,250,.16), transparent 70%);
    transform: rotate(-6deg); pointer-events: none;
  }
  header { position: relative; display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; }
  .kicker { font-size: 12px; letter-spacing: .3em; color: #7dd3fc; text-transform: uppercase; }
  h1 { margin: 8px 0 6px; font-size: 38px; line-height: 1.1; letter-spacing: .04em; }
  .sub { font-size: 14px; color: #a9b8d4; line-height: 1.6; max-width: 620px; }
  .stamp {
    flex: none; padding: 12px 16px; border-radius: 16px; text-align: right;
    background: linear-gradient(135deg, rgba(56,189,248,.14), rgba(167,139,250,.18));
    border: 1px solid rgba(255,255,255,.18); font-size: 12px; color: #cdd9f0;
  }
  .stamp strong { display: block; font-size: 22px; color: #facc15; letter-spacing: .04em; }
  .meta { display: flex; flex-wrap: wrap; gap: 8px; margin: 18px 0 16px; }
  .badge {
    padding: 7px 12px; border-radius: 999px; font-size: 12px; color: #d7e3fb;
    background: rgba(255,255,255,.07); border: 1px solid rgba(255,255,255,.14);
  }
  .badge.repo {
    color: #facc15; background: rgba(250,204,21,.08);
    border-color: rgba(250,204,21,.24);
    font-family: "Cascadia Mono", "Consolas", monospace;
  }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  .card {
    --accent: #38bdf8;
    position: relative; padding: 16px 18px 12px; border-radius: 20px;
    background: linear-gradient(180deg, rgba(255,255,255,.075), rgba(255,255,255,.045));
    border: 1px solid rgba(255,255,255,.12);
    box-shadow: inset 0 1px 0 rgba(255,255,255,.12);
  }
  .card::after {
    content: ""; position: absolute; left: 18px; right: 18px; top: 0; height: 2px;
    background: linear-gradient(90deg, var(--accent), transparent);
    opacity: .85;
  }
  .head { display: flex; align-items: center; gap: 10px; margin-bottom: 9px; }
  .icon {
    width: 34px; height: 34px; display: grid; place-items: center;
    border-radius: 11px; font-size: 17px; background: rgba(255,255,255,.08);
    border: 1px solid rgba(255,255,255,.14);
  }
  .title { font-size: 20px; font-weight: 700; letter-spacing: .02em; }
  .tag { margin-left: auto; font-size: 10px; letter-spacing: .16em; color: var(--accent); opacity: .9; }
  .row {
    display: grid; grid-template-columns: 1.08fr 1fr; gap: 10px; align-items: center;
    padding: 5px 0; border-bottom: 1px solid rgba(255,255,255,.07);
  }
  .row:last-child { border-bottom: 0; }
  .cmd {
    justify-self: start; max-width: 100%; padding: 3px 7px; border-radius: 7px;
    font-family: "Cascadia Mono", "Consolas", monospace; font-size: 12px;
    color: #bfe8ff; background: rgba(56,189,248,.1);
    border: 1px solid rgba(56,189,248,.18); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .desc { font-size: 12.5px; color: #a9b8d4; line-height: 1.35; }
  .ai { grid-column: 1 / -1; }
  .chips { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 4px; }
  .chip {
    padding: 5px 9px; border-radius: 8px; font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 11.5px; color: #d8ccff; background: rgba(167,139,250,.11);
    border: 1px solid rgba(167,139,250,.22);
  }
  footer {
    display: flex; align-items: center; justify-content: space-between;
    margin-top: 16px; padding-top: 12px; font-size: 12px; color: #8fa1c0;
    border-top: 1px solid rgba(255,255,255,.09);
  }
  footer strong { color: #facc15; }
  .repo-url {
    color: #bfe8ff; font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 11.5px; letter-spacing: .02em;
  }
  @media (max-width: 880px) {
    body { padding: 18px 14px 22px; }
    .shell { padding: 22px 20px 18px; }
    .grid { grid-template-columns: 1fr; }
    .cmd { white-space: normal; }
    h1 { font-size: 32px; }
    .stamp { display: none; }
  }
</style>
</head>
<body>
  <div class="shell">
    <header>
      <div>
        <div class="kicker">SHIFT BOARD · HELP</div>
        <h1>排班工坊 · 使用手册</h1>
        <div class="sub">月休 4 / 6 / 8 天自动排班，打卡算工时，时薪或月薪自动结算；发薪中心看应发 / 已到账 / 未发，发薪日到点提醒；说「今日考勤」单独弹图。
        指令可以发，也可以直接对机器人说，AI 会自己调用插件工具。</div>
      </div>
      <div class="stamp"><strong>22</strong>个 AI 工具</div>
    </header>
    <div class="meta">
      <span class="badge">🗓 自动排班</span>
      <span class="badge">⏱ 打卡工时</span>
      <span class="badge">💰 时薪 / 月薪</span>
      <span class="badge">🏦 发薪日 + 今日考勤图</span>
      <span class="badge">🔔 自定义提醒 + AI 天气</span>
      <span class="badge">🧠 Function Calling</span>
      <span class="badge repo">🐙 仓库 DwuHu/astrbot_plugin_shift_board</span>
    </div>
    <div class="grid">
<!--CARDS-->
<!--AI-->
    </div>
    <footer>
      <span>作者 <strong>DwuHu（九月月）</strong> · 数据只存本地</span>
      <span class="repo-url">https://github.com/DwuHu/astrbot_plugin_shift_board</span>
      <span>发送 <strong>/排班 帮助</strong> 随时唤出这张卡片</span>
    </footer>
  </div>
</body>
</html>
"""


def help_html() -> str:
    """帮助卡片 HTML：功能模块 + 22 个 AI 工具。"""
    cards = []
    for icon, title, tag, accent, rows in _HELP_GROUPS:
        body = "".join(
            f'<div class="row"><span class="cmd">{cmd}</span>'
            f'<span class="desc">{desc}</span></div>'
            for cmd, desc in rows
        )
        cards.append(
            f'<section class="card" style="--accent:{accent}">'
            f'<div class="head"><span class="icon">{icon}</span>'
            f'<span class="title">{title}</span><span class="tag">{tag}</span></div>'
            f"{body}</section>"
        )
    chips = "".join(f'<span class="chip">{name}</span>' for name in _AI_TOOL_NAMES)
    ai = (
        '<section class="card ai" style="--accent:#a78bfa">'
        '<div class="head"><span class="icon">🧠</span><span class="title">AI 可以直接调用</span>'
        '<span class="tag">FUNCTION CALLING</span></div>'
        '<div class="chips">' + chips + "</div>"
        '<div class="row" style="margin-top:8px;border-bottom:0">'
        '<span class="desc">AI 读数据不会改库；写排班、工资、发薪规则、到账登记、提醒、罚款、扣款和打卡才需要用户明确说明。'
        "删除打卡默认关闭；扣款金额与标签由用户决定，AI 不会自己编数字。</span></div></section>"
    )
    return _HELP_HTML.replace("<!--CARDS-->", "".join(cards)).replace("<!--AI-->", ai)


def header_text(user_nickname: str, profile: Profile) -> str:
    weekend = "周末优先" if profile.prefer_weekend else "均匀排休"
    stage = "实习期" if profile.employment_stage == "intern" else "正式"
    if profile.pay_mode == "monthly" and profile.monthly_salary > 0:
        pay_text = f"{stage} · 月薪 {_money(profile.monthly_salary)} · 加班 ×{profile.overtime_rate:g}"
    else:
        pay_text = f"{stage} · 时薪 {_money(profile.hourly_rate)}/h · 加班 ×{profile.overtime_rate:g}"
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
    if summary.get("leave_days") or summary.get("prejob_days"):
        lines.append(
            f"🗓 未上班 {summary.get('prejob_days', 0)} 天"
            f" · 请假 {summary.get('leave_days', 0)} 天（不计薪）"
        )
    if summary.get("today_minutes") or summary.get("today_open_start"):
        live = ""
        if summary.get("today_open_start"):
            live = f"（本段 {human_minutes(summary.get('today_open_minutes', 0))}）"
        remain = ""
        if summary.get("today_remaining_minutes"):
            remain = (
                f" · 距 {summary.get('today_end_label', '')} 下班 "
                f"{human_minutes(summary['today_remaining_minutes'])}"
            )
        lines.append(
            f"🕒 现在 {summary.get('now_label', '')} · 今天已上 "
            f"{human_minutes(summary.get('today_minutes', 0))}{live}{remain}"
        )
    if summary.get("stage"):
        lines.append(
            f"🧑‍💼 {summary.get('stage_label', '')} · 本月实际已挣 "
            f"{_money(summary.get('earned_pay', 0))}"
            f"（到手 {_money(summary.get('earned_net', 0))}，按已打卡天数累计）"
        )
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
            if summary.get("deduction_total"):
                money_line += f" - 固定扣款 {_money(summary['deduction_total'])}"
            money_line += f" = 到手 {_money(summary['total_pay'])}"
            lines.append(money_line)
            if summary.get("deduction_items"):
                detail = "、".join(
                    f"{item['name']} {_money(item['amount'])}"
                    for item in summary["deduction_items"]
                )
                lines.append(f"🧾 本月扣款：{detail}")
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
    elif day["kind"] == "leave":
        plan = f"{current.month:02d}/{current.day:02d} {weekday} 请假（不计薪）"
    elif day["kind"] == "prejob":
        plan = f"{current.month:02d}/{current.day:02d} {weekday} 未上班"
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
    if day["kind"] in {"leave", "prejob"}:
        return f"{icon} {plan}"
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
    template: str = "",
    weather: str = "",
    tomorrow_text: str = "",
) -> str:
    """上下班提醒文案。kind 取 in / out；template 非空时按用户自定义模板渲染。"""
    day_text = f"{day.month}月{day.day}日"
    greeting = "🌤 早安"
    if kind == "in":
        try:
            hour = int(str(start).split(":")[0])
        except (TypeError, ValueError):
            hour = 9
        greeting = "🌙 晚上好" if hour >= 18 or hour < 5 else "🌤 早安"
        default_lines = [
            f"{greeting}，{nickname}",
            f"今天 {start}-{end}，大概 {human_minutes(minutes)}。",
            f"距离上班还有 {before} 分钟，不用赶，先吃点东西，路上慢一点。",
            "到了发「/打卡 上班」，我帮你把今天记上。",
        ]
        punch_line = "到了发「/打卡 上班」，我帮你把今天记上。"
        type_label = "上班"
    else:
        default_lines = [
            f"🌇 快收工啦，{nickname}",
            f"{day_text} 班次 {start}-{end}，还有 {before} 分钟下班（{end}）。",
            f"你 {open_at or start} 的上班卡还在计时，走之前记得「/打卡 下班」。"
            if punch_state == "open"
            else "今天还没有完整的打卡记录，记得「/打卡 下班」或补录一次。",
            "打完下班卡，我马上把今天入账多少、本月累计多少算给你。",
        ]
        default_lines = [line for line in default_lines if line.strip()]
        punch_line = (
            f"你 {open_at or start} 的上班卡还在计时，走之前记得「/打卡 下班」。"
            if punch_state == "open"
            else "今天还没有完整的打卡记录，记得「/打卡 下班」或补录一次。"
        )
        type_label = "下班"
        greeting = "🌇 快收工啦"

    custom = str(template or "").strip()
    if custom:
        mapping = {
            "{nickname}": nickname,
            "{date}": day_text,
            "{day}": day_text,
            "{start}": start,
            "{end}": end,
            "{minutes}": human_minutes(minutes),
            "{hours}": human_minutes(minutes),
            "{before}": str(before),
            "{type}": type_label,
            "{greeting}": greeting,
            "{punch}": punch_line,
            "{weather}": weather,
            "{tomorrow}": tomorrow_text,
        }
        for token, value in mapping.items():
            custom = custom.replace(token, value)
        lines = [line for line in custom.splitlines() if line.strip()]
        if weather and "{weather}" not in str(template or ""):
            lines.append(weather)
        return "\n".join(lines)[:800]

    if weather:
        default_lines.append(weather)
    return "\n".join(default_lines)


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
            f" · 每日超 {profile.standard_hours:g}h 算加班"
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
            f" · 每日超 {profile.standard_hours:g}h 算加班"
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
    ]
    if summary.get("deduction_total"):
        for item in summary.get("deduction_items", []):
            tag = "每月" if item["repeat_monthly"] else "仅本月"
            lines.append(f"固定扣款 {item['name']}（{tag}） = -{_money(item['amount'])}")
        lines.append(f"固定扣款合计 = -{_money(summary['deduction_total'])}")
    lines.extend([
        "—" * 12,
        f"到手合计 {_money(summary['total_pay'])}",
        f"全月预计 {_money(summary['projected_pay'])} · 日薪 {_money(summary['daily_pay'])}",
    ])
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
    now_label: str = "",
    today_minutes: int = 0,
    today_remaining_minutes: int = 0,
    today_end_label: str = "",
) -> str:
    clock = f"现在 {now_label} · " if now_label else ""
    remain = (
        f" · 距 {today_end_label} 下班 {human_minutes(today_remaining_minutes)}"
        if today_remaining_minutes > 0 and today_end_label
        else ""
    )
    if open_punch:
        return (
            f"🟢 {user_nickname} 正在上班中\n"
            f"上班打卡 {open_punch['start']} · 本段已计时 "
            f"{open_punch['elapsed_minutes'] // 60}小时{open_punch['elapsed_minutes'] % 60:02d}分\n"
            f"🕒 {clock}今天已上 {human_minutes(today_minutes)}{remain}"
        )
    lines = [
        f"⚪ {user_nickname} 当前未在班",
        f"🕒 {clock}今天已上 {human_minutes(today_minutes)}{remain}",
        header_text(user_nickname, profile),
        "",
    ]
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
  .cell.leave { background: rgba(167,139,250,.14); border-color: rgba(167,139,250,.38); }
  .cell.prejob { background: rgba(100,116,139,.1); border-color: rgba(148,163,184,.24); opacity:.86; }
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
      <div class="metric money"><span>到手工资</span><strong>{{ total_pay }}</strong></div>
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
        elif day["kind"] == "leave":
            cls = "leave"
            tag = "请假 · 不计薪"
        elif day["kind"] == "prejob":
            cls = "prejob"
            tag = "未上班"
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


def payroll_center_text(payroll: dict, nickname: str) -> str:
    """聊天里的发薪中心文字版。"""
    lines = [
        f"💰 发薪中心 · {nickname}",
        f"📅 规则：{payroll.get('rule_label', '当月月底发')}"
        + (
            f" · 发薪日提醒 {payroll.get('remind_time', '17:00')}"
            if payroll.get("remind_enabled", True)
            else " · 发薪日提醒已关"
        ),
    ]
    next_item = payroll.get("next")
    if next_item:
        days = int(next_item.get("days_left", 0))
        when = (
            f"已过 {-days} 天"
            if days < 0
            else ("就是今天" if days == 0 else f"还有 {days} 天")
        )
        lines.append(
            f"⏳ 下一笔：{next_item.get('month_label', '')}工资 "
            f"{_money(next_item.get('unpaid', 0))} · "
            f"{next_item.get('payday_label', '')}发（{when}）"
        )
    else:
        lines.append("⏳ 下一笔：目前没有待发工资")
    lines.append(
        f"🧾 未发合计 {_money(payroll.get('unpaid_total', 0))}"
        f"（已到发薪日 {_money(payroll.get('due_unpaid', 0))}"
        f" · 未来待发 {_money(payroll.get('upcoming_unpaid', 0))}）"
    )
    entries = list(payroll.get("entries") or [])
    if not entries:
        lines.append("还没有可计算的工资月份，先排班 / 设置工资。")
        return "\n".join(lines)
    lines.append("")
    icon = {"paid": "✅", "partial": "◐", "due": "⚠️", "pending": "·"}
    for item in reversed(entries):
        status = str(item.get("status") or "pending")
        lines.append(
            f"{icon.get(status, '·')} {item.get('month_label', '')}工资"
            f"（{item.get('stage_label', '')}）"
            f" 应发 {_money(item.get('due', 0))}"
            f" · 已到账 {_money(item.get('paid', 0))}"
            f" · 未发 {_money(item.get('unpaid', 0))}"
            f" · {item.get('payday_label', '')}发"
            f"【{item.get('status_label', '')}】"
        )
    lines.append("")
    lines.append("登记到账：/发薪 到账 2026-10 [实发金额]")
    lines.append("撤销登记：/发薪 撤销 2026-10")
    return "\n".join(lines)


def attendance_html(data: dict) -> str:
    """今日考勤独立卡片：当天工时、今日按时薪累计收入、距下班、本月/上月应发与未发。"""
    nickname = escape(str(data.get("nickname") or "打工人"))
    prev_label = escape(str(data.get("prev_label") or "上月"))
    today_status = escape(str(data.get("today_status") or "还没上班"))
    status_class = escape(str(data.get("status_class") or "idle"))
    return f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8" />
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;700;900&display=swap');
*{{box-sizing:border-box;margin:0;padding:0}}
body{{width:760px;padding:26px;background:
  radial-gradient(circle at 12% 0%, rgba(56,189,248,.32), transparent 36%),
  radial-gradient(circle at 92% 12%, rgba(244,114,182,.26), transparent 34%),
  linear-gradient(150deg,#07111f 0%,#0b1828 46%,#111827 100%);
  font-family:'Noto Sans SC',-apple-system,'Microsoft YaHei',sans-serif;color:#e6f1ff}}
.card{{position:relative;overflow:hidden;border-radius:30px;padding:28px;
  background:linear-gradient(145deg,rgba(18,33,52,.88),rgba(10,20,34,.92));
  border:1px solid rgba(148,199,255,.18);
  box-shadow:0 24px 70px rgba(0,0,0,.42), inset 0 1px 0 rgba(255,255,255,.08)}}
.orb{{position:absolute;border-radius:50%;filter:blur(2px);opacity:.5;pointer-events:none}}
.orb.a{{width:190px;height:190px;right:-64px;top:-62px;background:radial-gradient(circle,#60a5fa,transparent 68%)}}
.orb.b{{width:150px;height:150px;left:-58px;bottom:-58px;background:radial-gradient(circle,#f472b6,transparent 70%)}}
.head{{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;position:relative;z-index:1}}
.kicker{{font-size:11px;letter-spacing:3px;color:#7dd3fc;font-weight:700}}
h1{{font-size:30px;line-height:1.2;margin-top:6px;letter-spacing:.5px}}
.name{{font-size:13px;color:#94a3b8;margin-top:7px}}
.date{{text-align:right;color:#9fb4cc;font-size:12px;line-height:1.9}}
.date b{{display:block;color:#f8fafc;font-size:20px;font-weight:800}}
.status{{display:inline-flex;align-items:center;gap:6px;margin-top:7px;padding:6px 12px;border-radius:999px;
  font-size:12px;font-weight:700;border:1px solid rgba(255,255,255,.1)}}
.status.live{{background:rgba(34,197,94,.15);color:#86efac;border-color:rgba(34,197,94,.34)}}
.status.done{{background:rgba(56,189,248,.12);color:#93c5fd}}
.status.rest{{background:rgba(148,163,184,.12);color:#cbd5e1}}
.status.idle{{background:rgba(250,204,21,.12);color:#fde68a}}
.hero{{display:block;margin-top:20px;position:relative;z-index:1}}
.glass{{border-radius:20px;padding:18px;background:rgba(255,255,255,.045);
  border:1px solid rgba(148,199,255,.14);backdrop-filter:blur(14px)}}
.label{{font-size:11px;letter-spacing:1.5px;color:#8ba3bd;font-weight:700}}
.big{{font-size:42px;font-weight:900;line-height:1;margin-top:9px;
  background:linear-gradient(120deg,#f8fafc,#93c5fd 55%,#c4b5fd);-webkit-background-clip:text;background-clip:text;color:transparent}}
.sub{{font-size:12px;color:#9fb4cc;margin-top:9px;line-height:1.7}}
.earn{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:11px;
  padding:10px 13px;border-radius:13px;background:rgba(52,211,153,.085);
  border:1px solid rgba(52,211,153,.2)}}
.earn-k{{font-size:11px;letter-spacing:1px;color:#8ba3bd;font-weight:700}}
.earn-v{{font-size:23px;font-weight:900;color:#86efac;line-height:1.1}}
.earn-f{{margin-left:auto;font-size:11px;color:#9fb4cc;text-align:right;line-height:1.5}}
.due{{font-size:13px;color:#cbd5e1;margin-top:12px}}
.due strong{{color:#fbbf24;font-size:24px;font-weight:800;margin-left:4px}}
.three{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:14px;position:relative;z-index:1}}
.three .glass{{padding:15px 14px}}
.three .money{{font-size:22px;margin-top:7px;white-space:nowrap}}
.three .sub{{font-size:11px;margin-top:7px;line-height:1.55}}
.money{{font-size:25px;font-weight:800;color:#f8fafc;margin-top:8px;letter-spacing:.2px}}
.money.warn{{color:#fbbf24}}
.money.ok{{color:#86efac}}
.prev{{margin-top:12px;padding:14px 16px;position:relative;z-index:1}}
.prev-head{{display:flex;align-items:center;justify-content:space-between;gap:12px}}
.prev-tag{{font-size:11px;font-weight:800;color:#93c5fd;letter-spacing:1px}}
.prev-row{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:9px}}
.prev-row .k{{font-size:10px;color:#8ba3bd;letter-spacing:1px}}
.prev-row .v{{font-size:15px;font-weight:800;color:#e2e8f0;margin-top:3px}}
.prev-row .v.warn{{color:#fbbf24}}
.foot{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:12px;position:relative;z-index:1}}
.foot .glass{{padding:13px 15px}}
.foot .v{{font-size:16px;font-weight:800;color:#e2e8f0;margin-top:5px}}
.foot .s{{font-size:11px;color:#9fb4cc;margin-top:5px}}
.hint{{margin-top:16px;padding-top:14px;border-top:1px solid rgba(148,199,255,.12);
  font-size:11px;color:#7f95ad;position:relative;z-index:1;display:flex;justify-content:space-between;gap:12px}}
</style></head>
<body>
<div class="card">
  <span class="orb a"></span><span class="orb b"></span>
  <div class="head">
    <div>
      <div class="kicker">TODAY ATTENDANCE</div>
      <h1>今日考勤</h1>
      <div class="name">{nickname} · 排班工坊</div>
    </div>
    <div class="date"><b>{escape(str(data.get("date_label") or ""))}</b>{escape(str(data.get("now_label") or ""))}
      <div class="status {status_class}">● {today_status}</div>
    </div>
  </div>

  <div class="hero">
    <div class="glass">
      <div class="label">今天已上</div>
      <div class="big">{escape(str(data.get("today_minutes_label") or "0小时"))}</div>
      <div class="sub">{escape(str(data.get("today_detail") or "今天还没有打卡"))}</div>
      <div class="earn">
        <span class="earn-k">今天已获得</span>
        <span class="earn-v">{escape(str(data.get("today_earned") or "¥0.00"))}</span>
        <span class="earn-f">按 {escape(str(data.get("today_hourly") or "¥0.00/小时"))}<br>已上 {escape(str(data.get("today_hours_value") or "0.0h"))} 累计</span>
      </div>
      <div class="due">距下班还剩 <strong>{escape(str(data.get("today_remaining_label") or "—"))}</strong>
        <span style="float:right;color:#93a3c0;font-size:12px">本月还要上 {escape(str(data.get("remaining_work") or "0.0h"))}</span></div>
    </div>
  </div>

  <div class="three">
    <div class="glass">
      <div class="label">本月应得</div>
      <div class="money">{escape(str(data.get("month_due") or "—"))}</div>
      <div class="sub">按整月口径<br>固定整月，不逐日变</div>
    </div>
    <div class="glass" style="background:linear-gradient(135deg,rgba(52,211,153,.10),rgba(255,255,255,.03))">
      <div class="label">本月实得</div>
      <div class="money ok">{escape(str(data.get("month_earned") or "¥0.00"))}</div>
      <div class="sub">{escape(str(data.get("month_progress") or "按已出勤天数累计"))}</div>
    </div>
    <div class="glass" style="background:linear-gradient(135deg,rgba(250,204,21,.09),rgba(255,255,255,.03))">
      <div class="label">本月未发</div>
      <div class="money warn">{escape(str(data.get("month_unpaid") or "¥0.00"))}</div>
      <div class="sub">已到账 {escape(str(data.get("month_paid") or "¥0.00"))}<br>按实得算，未到发薪日</div>
    </div>
  </div>

  <div class="glass prev">
    <div class="prev-head">
      <div class="label">{prev_label} · 已固定</div>
      <div class="prev-tag">上月按整月固定</div>
    </div>
    <div class="prev-row">
      <div><div class="k">应发</div><div class="v">{escape(str(data.get("prev_due") or "—"))}</div></div>
      <div><div class="k">已到账</div><div class="v">{escape(str(data.get("prev_paid") or "—"))}</div></div>
      <div><div class="k">未发</div><div class="v warn">{escape(str(data.get("prev_unpaid") or "—"))}</div></div>
    </div>
  </div>

  <div class="foot">
    <div class="glass"><div class="label">所有未发合计</div>
      <div class="v money warn" style="font-size:17px;margin-top:5px">{escape(str(data.get("unpaid_total") or "¥0.00"))}</div>
      <div class="s">上月及以前 {escape(str(data.get("older_unpaid") or "¥0.00"))} · 本月实得未发 {escape(str(data.get("month_unpaid") or "¥0.00"))}</div>
    </div>
    <div class="glass"><div class="label">下次发薪</div><div class="v">{escape(str(data.get("next_pay_label") or "暂无"))}</div>
      <div class="s">待发 {escape(str(data.get("next_amount") or "¥0.00"))}</div>
    </div>
    <div class="glass"><div class="label">发薪规则</div><div class="v" style="font-size:13px">{escape(str(data.get("rule_label") or ""))}</div>
      <div class="s">到点提醒工资到账</div>
    </div>
  </div>

  <div class="hint"><span>{escape(str(data.get("month_stats") or ""))}</span><span>数据来自排班与打卡 · 实时</span></div>
</div>
</body></html>"""