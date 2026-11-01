"""邮件提醒：默认不发送、设置校验与授权码不外泄、SMTP 发送（用假的 smtplib）、恐慌日 / ETF / 持仓邮件内容、同一事件同一天只发一次。"""
import smtplib
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

from server import db, market_calendar as mc, notify, settings  # noqa: E402


def _ready(**kw):
    c = settings.base_cfg().setdefault("notify", {})
    c.update(enabled=True, sender="me@qq.com", password="s3cret-code", recipients="", host="smtp.qq.com", port=465, security="ssl",
             events={"panic": True, "etf": True, "positions": True}, **kw)


class FakeSMTP:
    sent, logins, fail = [], [], None

    def __init__(self, host, port, timeout=None, context=None):
        FakeSMTP.host, FakeSMTP.port = host, port

    def ehlo(self): pass
    def starttls(self, context=None): pass

    def login(self, user, pw):
        if FakeSMTP.fail == "auth":
            raise smtplib.SMTPAuthenticationError(535, b"bad")
        FakeSMTP.logins.append((user, pw))

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)

    def quit(self): pass


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.logins, FakeSMTP.fail = [], [], None
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def test_default_is_silent(demo_env, smtp):
    """没设置 / 没打开开关：不发任何邮件，盘后链路直接返回空。"""
    assert notify.public_config()["ready"] is False and "不会发送" in notify.public_config()["status"]
    assert notify.send_mail("x", "y") == (False, "邮件提醒没有打开")
    assert notify.after_scan("CN", "2026-09-30", {"summary": {"panic": {"active": True, "run": {"n": 1}}}}) == {}
    assert smtp.sent == []
    settings.base_cfg()["notify"].update(enabled=True)                      # 打开了但缺发件邮箱 / 授权码 → 仍不发
    assert notify.is_ready()[0] is False and notify.after_scan("CN", "2026-09-30", {}) == {} and smtp.sent == []


def test_save_validates_hides_password_and_autofills(demo_env):
    with pytest.raises(ValueError):
        notify.save_config({"sender": "not-an-email"})
    with pytest.raises(ValueError):
        notify.save_config({"sender": "me@163.com", "recipients": "a@b.com, bad"})
    pub = notify.save_config({"enabled": True, "sender": "me@163.com", "password": "abc123"})
    assert pub["host"] == "smtp.163.com" and pub["port"] == 465 and pub["security"] == "ssl" and pub["password_set"] is True and pub["ready"] is True
    assert "abc123" not in str(pub) and "password" not in pub                                      # 接口返回里没有授权码
    notify.save_config({"sender": "me@163.com", "password": ""})                                     # 留空 = 保持不变
    assert settings.base_cfg()["notify"]["password"] == "abc123"
    assert notify.save_config({"clear_password": True})["ready"] is False
    assert notify.recipients(settings.base_cfg()["notify"]) == ["me@163.com"]                       # 收件人留空 = 发给自己


def test_notify_api_never_returns_password(demo_env):
    from server import main
    c = TestClient(main.app)
    r = c.put("/api/notify", json={"enabled": True, "sender": "me@qq.com", "password": "topsecret"})
    assert r.status_code == 200 and "topsecret" not in r.text and r.json()["password_set"] is True
    assert "topsecret" not in c.get("/api/notify").text and "topsecret" not in c.get("/api/settings").text
    assert c.put("/api/notify", json={"sender": "bad"}).status_code == 400
    assert c.get("/api/notify/preset", params={"email": "x@gmail.com"}).json()["host"] == "smtp.gmail.com"


def test_send_mail_success_and_friendly_errors(demo_env, smtp):
    _ready()
    ok, msg = notify.send_mail("主题", "正文：中文 OK")
    assert ok and "me@qq.com" in msg and smtp.logins == [("me@qq.com", "s3cret-code")]
    m = smtp.sent[0]
    assert m["To"] == "me@qq.com" and m["Subject"] == "主题" and "中文 OK" in m.get_content()
    smtp.fail = "auth"
    ok, msg = notify.send_mail("a", "b")
    assert not ok and "授权码" in msg and "s3cret-code" not in msg

    class Boom(FakeSMTP):
        def __init__(self, *a, **k):
            raise OSError("network down")
    smtplib.SMTP_SSL = Boom
    ok, msg = notify.send_mail("a", "b")
    assert not ok and "发送失败" in msg and "s3cret-code" not in msg


def test_message_contents():
    p = {"active": True, "trigger": "b20", "b20": 0.136, "cut": 0.15, "run": {"n": 3, "start": "2026-10-28"},
         "etf": {"items": [{"name": "沪深300", "etf": "510300", "hold": {"5": {"diff_mean": 0.0129}, "20": {"diff_mean": 0.021}}}]}}
    subj, body = notify.panic_message("CN", p)
    assert "A股恐慌日第 3 天" in subj and "1/10" in body and "第 3/10 份" in body and "13.6%" in body and "沪深300" in body and "+1.3%" in body
    assert "别在第一个恐慌日一次买完" in body and "5 天以内" in body and "QDII" not in body
    assert notify.panic_message("CN", {**p, "run": {"n": 11}}) is None and notify.panic_message("CN", {"active": False}) is None
    us = {**p, "trigger": "vix", "vix": 27.3, "cut": 25, "b20": None}
    assert "VIX 27.3" in notify.panic_message("US", us)[1] and "QDII" in notify.panic_message("US", us)[1]
    mv = {"status": "ok", "indices": [{"name": "沪深300", "next_open": {"actions": ["开盘买入趋势仓（50%）"]}}, {"name": "创业板指", "next_open": {"actions": []}}]}
    s2, b2 = notify.etf_message("CN", mv)
    assert "1 个信号" in s2 and "沪深300：开盘买入趋势仓" in b2 and "创业板指" not in b2
    assert notify.etf_message("CN", {"status": "ok", "indices": [{"name": "x", "next_open": {"actions": []}}]}) is None
    h = {"positions": [{"level": "must", "symbol": "sh.600000", "name": "浦发银行", "action": "止损", "new_stop": 9.5}, {"level": "ok", "symbol": "x"}]}
    s3, b3 = notify.positions_message("CN", h)
    assert "1 只需要处理" in s3 and "浦发银行" in b3 and "9.5" in b3 and notify.positions_message("CN", {"positions": []}) is None


def test_after_scan_sends_once_per_day_and_skips_history(demo_env, smtp, monkeypatch):
    _ready()
    with db.market_db("CN") as c:
        last = mc.last_closed_trading_day(c, "CN")
        older = mc.trading_days(c)[-10]
    scan = {"summary": {"panic": {"active": True, "trigger": "b20", "b20": 0.1, "cut": 0.15, "run": {"n": 2, "start": last}, "etf": {"items": []}}}}
    monkeypatch.setattr("server.market_view.build", lambda market: {"status": "no_data"})
    r1 = notify.after_scan("CN", last, scan, {"positions": []})
    assert r1["panic"] == "已发送" and r1["etf"] == "没有事件" and r1["positions"] == "没有事件" and len(smtp.sent) == 1
    r2 = notify.after_scan("CN", last, scan, {"positions": []})                                    # 同一天再跑：不重复发
    assert r2["panic"] == "已发过" and len(smtp.sent) == 1
    assert notify.after_scan("CN", older, scan, None) == {"skipped": "补跑的历史交易日，不发提醒"} and len(smtp.sent) == 1
    settings.base_cfg()["notify"]["events"]["panic"] = False                                         # 关掉恐慌日事件
    assert "panic" not in notify.after_scan("CN", last, scan, None)
