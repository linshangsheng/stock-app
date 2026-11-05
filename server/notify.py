"""邮件提醒（默认不发送）。设置页填好「发件邮箱 + SMTP 授权码 + 收件邮箱」并打开开关之后，盘后任务链扫描完成时检查一次：
  · 恐慌日：A 股温度 < 15% / 美股 VIX >= 25 时，告诉你「本轮第几个恐慌日、今天买预算的 1/10」（最多提醒到第 10 个恐慌日）
  · ETF 规则：明天开盘宽基 ETF 有买入 / 卖出信号
  · 持仓：有「必须处理」的持仓（止损 / 止盈 / 上移止损）
同一类事件同一个交易日只发一封（记在行情库 meta 里）；补跑历史交易日时不发；任何一步出错都只记日志、不影响盘后任务链。
没有设置、没打开开关、或缺少任何一项（邮箱 / 授权码 / 服务器）时一律不发送。
授权码保存在 data/user_config.yaml（本机文件），不会出现在接口返回、日志和每日备份里。授权码不是邮箱的登录密码——各家邮箱要在网页设置里单独生成。"""
from __future__ import annotations

import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from . import db, market_calendar as mc, settings

log = logging.getLogger(__name__)
MARKET_NAME = {"CN": "A股", "US": "美股"}
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
MAX_PANIC_MAILS = 10                      # 与「每个恐慌日买 1/10、最多 10 份」的计划一致

# 常见邮箱的 SMTP 服务器：(域名, 服务器, 端口, 加密, 怎么拿授权码)
PRESETS = [
    (("qq.com", "foxmail.com"), "smtp.qq.com", 465, "ssl", "QQ 邮箱：网页版「设置 → 账号」里开启 POP3/SMTP 服务，生成「授权码」，填授权码（不是 QQ 密码）。"),
    (("163.com",), "smtp.163.com", 465, "ssl", "163 邮箱：网页版「设置 → POP3/SMTP/IMAP」开启 SMTP 服务，设置「客户端授权密码」，填它（不是登录密码）。"),
    (("126.com",), "smtp.126.com", 465, "ssl", "126 邮箱：网页版「设置 → POP3/SMTP/IMAP」开启 SMTP 服务，设置「客户端授权密码」，填它（不是登录密码）。"),
    (("yeah.net",), "smtp.yeah.net", 465, "ssl", "网易邮箱：开启 SMTP 服务并设置「客户端授权密码」，填它。"),
    (("gmail.com", "googlemail.com"), "smtp.gmail.com", 465, "ssl", "Gmail：先开启两步验证，再创建「应用专用密码」（16 位），填它（不是登录密码）。"),
    (("outlook.com", "hotmail.com", "live.com"), "smtp-mail.outlook.com", 587, "starttls", "Outlook / Hotmail 近年限制了密码登录，可能发不出去；优先用 QQ / 163 / Gmail。需要「应用密码」。"),
    (("sina.com", "sina.cn"), "smtp.sina.com", 465, "ssl", "新浪邮箱：设置里开启 SMTP 并生成授权码。"),
    (("aliyun.com",), "smtp.aliyun.com", 465, "ssl", "阿里邮箱：开启 SMTP，使用邮箱密码或授权码。"),
    (("icloud.com", "me.com"), "smtp.mail.me.com", 587, "starttls", "iCloud：在 Apple ID 里生成「App 专用密码」，填它。"),
]


def preset_for(email: str) -> dict | None:
    dom = (email or "").rsplit("@", 1)[-1].lower().strip()
    for doms, host, port, sec, tip in PRESETS:
        if dom in doms:
            return {"host": host, "port": port, "security": sec, "tip": tip}
    return None


def _cfg() -> dict:
    return dict(settings.base_cfg().get("notify") or {})


def recipients(c: dict | None = None) -> list[str]:
    c = c or _cfg()
    raw = (c.get("recipients") or "").strip() or (c.get("sender") or "")
    return [x for x in re.split(r"[,;\s]+", raw) if x]


def is_ready(c: dict | None = None) -> tuple[bool, str]:
    """能不能发：必须打开开关，且发件邮箱、授权码、服务器、收件邮箱都有。"""
    c = c or _cfg()
    if not c.get("enabled"):
        return False, "邮件提醒没有打开"
    if not EMAIL_RE.match(c.get("sender") or ""):
        return False, "还没有填发件邮箱"
    if not c.get("password"):
        return False, "还没有填授权码"
    if not c.get("host"):
        return False, "还没有填 SMTP 服务器（常见邮箱会按发件邮箱自动填好）"
    rc = recipients(c)
    if not rc or not all(EMAIL_RE.match(x) for x in rc):
        return False, "收件邮箱格式不对"
    return True, "ok"


def public_config() -> dict:
    """给设置页用：不含授权码，只说有没有。"""
    c = _cfg()
    ready, why = is_ready(c)
    return {"enabled": bool(c.get("enabled")), "sender": c.get("sender") or "", "password_set": bool(c.get("password")),
            "recipients": c.get("recipients") or "", "host": c.get("host") or "", "port": int(c.get("port") or 465),
            "security": c.get("security") or "ssl", "events": {"panic": True, "etf": True, "positions": True, **(c.get("events") or {})},
            "ready": ready, "status": "会发送" if ready else f"不会发送：{why}", "preset": preset_for(c.get("sender") or "")}


def save_config(body: dict) -> dict:
    """保存设置页提交的内容。授权码留空 = 保持原来的；host / port / security 留空时按发件邮箱自动填。"""
    cur = _cfg()
    new = dict(cur)
    for k in ("enabled",):
        if k in body:
            new[k] = bool(body[k])
    for k in ("sender", "recipients", "host", "security"):
        if k in body:
            new[k] = str(body[k] or "").strip()
    if "port" in body and str(body["port"]).strip():
        new["port"] = int(body["port"])
    if body.get("password"):
        new["password"] = str(body["password"]).strip()
    elif body.get("clear_password"):
        new["password"] = ""
    if isinstance(body.get("events"), dict):
        new["events"] = {k: bool(v) for k, v in body["events"].items() if k in ("panic", "etf", "positions")}
    if new.get("sender") and not EMAIL_RE.match(new["sender"]):
        raise ValueError("发件邮箱格式不对")
    if new.get("recipients") and not all(EMAIL_RE.match(x) for x in re.split(r"[,;\s]+", new["recipients"]) if x):
        raise ValueError("收件邮箱格式不对（多个用逗号分隔）")
    if new.get("security") not in ("ssl", "starttls", "none"):
        new["security"] = "ssl"
    p = preset_for(new.get("sender") or "")
    if p and not new.get("host"):
        new.update(host=p["host"], port=p["port"], security=p["security"])
    settings.save_user_config({"notify": new})
    return public_config()


def send_mail(subject: str, body: str, c: dict | None = None) -> tuple[bool, str]:
    """发一封纯文本邮件。返回 (是否成功, 说明)；不抛异常，说明里不含授权码。"""
    c = c or _cfg()
    ready, why = is_ready(c)
    if not ready:
        return False, why
    rc = recipients(c)
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = c["sender"], ", ".join(rc), subject
    msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid()
    msg.set_content(body, charset="utf-8")
    host, port, sec = c["host"], int(c.get("port") or 465), c.get("security") or "ssl"
    try:
        ctx = ssl.create_default_context()
        if sec == "ssl":
            s = smtplib.SMTP_SSL(host, port, timeout=25, context=ctx)
        else:
            s = smtplib.SMTP(host, port, timeout=25)
            s.ehlo()
            if sec == "starttls":
                s.starttls(context=ctx)
                s.ehlo()
        try:
            s.login(c["sender"], c["password"])
            s.send_message(msg)
        finally:
            try:
                s.quit()
            except Exception:  # noqa: BLE001 - 断开失败不影响结果
                pass
    except smtplib.SMTPAuthenticationError:
        return False, "登录失败：多半是授权码填错了（要填邮箱网页里单独生成的「授权码 / 应用专用密码」，不是登录密码）"
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        return False, f"发送失败：{type(e).__name__}: {str(e)[:120]}（检查服务器、端口和加密方式，或网络是否能连到邮箱服务器）"
    return True, f"已发送到 {', '.join(rc)}"


# ---- 邮件内容 ----------------------------------------------------------------

def _sgn(v) -> str:
    return "—" if v is None else f"{v * 100:+.1f}%"


def panic_message(market: str, p: dict | None) -> tuple[str, str] | None:
    """恐慌日提醒。今天不是恐慌日、或本轮已经买满 10 份（第 11 个恐慌日起）时返回 None。"""
    if not p or not p.get("active"):
        return None
    run = p.get("run") or {}
    n = int(run.get("n") or 1)
    if n > MAX_PANIC_MAILS:
        return None
    name = MARKET_NAME.get(market, market)
    if p.get("trigger") == "vix":
        trig = f"VIX {p.get('vix'):.1f}（不低于 {p.get('cut'):g}）"
    else:
        trig = f"全市场温度 {(p.get('b20') or 0) * 100:.1f}%（低于 {(p.get('cut') or 0) * 100:.0f}%）"
    lines = [f"{name}恐慌日：{trig}", f"本轮恐慌第 {n} 个恐慌日（{run.get('start', '今天')} 起）。", "",
             f"按软件的计划：今天买「你打算投入的预算」的 1/10（第 {n}/10 份）；恐慌结束（不再触发）就停，用不完的钱照常处理。", "",
             "对应的宽基指数 / ETF（历史上恐慌日之后，比平时多出的平均涨跌）："]
    for it in (p.get("etf") or {}).get("items", []):
        h5, h20 = (it.get("hold") or {}).get("5") or {}, (it.get("hold") or {}).get("20") or {}
        lines.append(f"  · {it['name']}（{it.get('etf', '')}）：5 天 {_sgn(h5.get('diff_mean'))}，20 天 {_sgn(h20.get('diff_mean'))}")
    lines += ["", "要点：",
              "  - 别在第一个恐慌日一次买完：长历史检验里这样买没有优势，2008、2015、2020 年曾在 20 天内亏 17%~31%。",
              "  - 每个恐慌日买一小份、最多买 10 天；恐慌往往只持续几天，预算常常用不完。"]
    if market == "US":
        lines.append("  - 在国内账户买美股 ETF（513500 / 513100 等 QDII）有溢价和汇率风险，买入价可能明显偏离净值。")
    else:
        lines.append("  - A 股只建议把它当 5 天以内的依据：2005~2016 的检验里持有 20 天没有优势。")
    lines += ["", "这是历史统计，不是保证，也不构成投资建议。打开软件「选股 → 恐慌日」查看详情。"]
    return f"【顺势】{name}恐慌日第 {n} 天：宽基 ETF 可以分批加仓（今天买预算的 1/10）", "\n".join(lines)


def etf_message(market: str, mv: dict | None) -> tuple[str, str] | None:
    """明天开盘宽基 ETF 规则有买入 / 卖出信号时才发。"""
    if not mv or mv.get("status") != "ok":
        return None
    acts = [f"{i['name']}：{'、'.join(i['next_open']['actions'])}" for i in mv.get("indices", []) if (i.get("next_open") or {}).get("actions")]
    if not acts:
        return None
    name = MARKET_NAME.get(market, market)
    body = "\n".join([f"{name}宽基 ETF 规则（只看收盘，开盘按市价成交，高开低开都一样）：", "", *[f"  · {a}" for a in acts], "",
                      "规则的买入位、止损位和回测证据见软件「选股」页首页的「明天要做什么」和「行情」页。", "", "这是规则输出，不构成投资建议。"])
    return f"【顺势】{name}宽基 ETF：明天开盘有 {len(acts)} 个信号", body


def positions_message(market: str, health: dict | None) -> tuple[str, str] | None:
    """有「必须处理」的持仓（止损 / 止盈 / 上移止损）时才发。"""
    must = [o for o in (health or {}).get("positions", []) if o.get("level") == "must"]
    if not must:
        return None
    name = MARKET_NAME.get(market, market)
    lines = [f"{name}持仓体检：{len(must)} 只需要处理（次日执行）", ""]
    for o in must:
        stop = o.get("new_stop") or o.get("current_stop")
        lines.append(f"  · {o.get('name') or o['symbol']}（{o['symbol']}）：{o.get('action', '')}" + (f"；止损价 {stop:g}" if stop else ""))
    lines += ["", "具体价格和股数见软件「持仓」页。券商里的条件单以你设置的为准。"]
    return f"【顺势】{name}持仓：{len(must)} 只需要处理", "\n".join(lines)


def after_scan(market: str, day: str, scan: dict, health: dict | None = None) -> dict:
    """盘后任务链扫描完成后调用。返回 {事件: 结果}；没设置 / 没打开时返回 {}。同一事件同一个交易日只发一次。"""
    c = _cfg()
    ready, _ = is_ready(c)
    if not ready:
        return {}
    ev = {"panic": True, "etf": True, "positions": True, **(c.get("events") or {})}
    out: dict = {}
    with db.market_db(market) as conn:
        closed = mc.last_closed_trading_day(conn, market)
        if closed and day < closed:
            return {"skipped": "补跑的历史交易日，不发提醒"}
        builders = {
            "panic": lambda: panic_message(market, (scan.get("summary") or {}).get("panic")),
            "etf": lambda: etf_message(market, __import__("server.market_view", fromlist=["build"]).build(market)),
            "positions": lambda: positions_message(market, health),
        }
        for kind, build in builders.items():
            if not ev.get(kind):
                continue
            key = f"notify:{kind}:{day}"
            if db.get_meta(conn, key):
                out[kind] = "已发过"
                continue
            try:
                msg = build()
            except Exception as e:  # noqa: BLE001 - 单个事件出错不影响其他
                out[kind] = f"出错：{type(e).__name__}"
                log.warning("notify build %s failed: %s", kind, e)
                continue
            if not msg:
                out[kind] = "没有事件"
                continue
            ok, why = send_mail(msg[0], msg[1], c)
            out[kind] = "已发送" if ok else f"发送失败：{why}"
            if ok:
                db.set_meta(conn, key, "sent")
            db.log_job(conn, "notify", "ok" if ok else "fail", f"{kind}：{msg[0]}" if ok else f"{kind}：{why}")
    return out
