"""账号与登录（server/accounts.py + main.py 的中间件和 /api/auth、/api/accounts）：管理员 root、建 / 暂停 / 重置 / 删账号、
一个账号只能一处登录、防猜密码、个人数据按账号分开、普通账号不能用管理员接口、备份包含账号库和各账号个人库。"""
import json
import sqlite3
import warnings
from types import SimpleNamespace

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

from server import accounts, db, notify, settings  # noqa: E402

LOCAL = ("127.0.0.1", 50000)        # 本机直接打开
REMOTE = ("8.8.8.8", 50000)          # 局域网 / 公网来的


@pytest.fixture
def main(demo_env, monkeypatch):
    monkeypatch.delenv("STOCK_AUTH")             # 本文件的测试要真的登录
    from server import main as m
    return m


def device(main, host=REMOTE):
    return TestClient(main.app, client=host)


def login(c, user="root", pw="123456"):
    return c.post("/api/auth/login", json={"username": user, "password": pw})


@pytest.fixture
def admin(main):
    c = device(main, LOCAL)
    assert login(c).status_code == 200
    return c


def new_user(admin, name="alice", pw="temp123", host=REMOTE, change_to="alice-pass"):
    """管理员建账号 → 对方第一次登录 → 必须改密码 → 改好。返回已登录、能用的客户端。"""
    assert admin.post("/api/accounts", json={"username": name, "password": pw}).status_code == 200
    c = TestClient(admin.app, client=host)
    assert login(c, name, pw).json()["must_change"] is True
    assert c.post("/api/auth/password", json={"old": pw, "new": change_to}).status_code == 200
    return c


# ---------------------------------------------------------------- 管理员与默认密码
def test_default_admin_is_root_and_data_stays_in_the_old_portfolio_db(main, admin):
    me = admin.get("/api/auth/me").json()
    assert me["auth"] is True and me["user"]["username"] == "root" and me["user"]["role"] == "admin" and me["user"]["id"] == 1
    assert me["default_password"] is True and me["must_change"] is False                    # 本机直接打开：只提醒，不强制
    assert db.portfolio_path(1) == settings.data_dir() / "portfolio.db"                        # 管理员沿用原来的个人库（自选股、持仓原样在里面）


def test_default_password_must_be_changed_when_not_opened_directly_on_this_machine(main):
    proxied = TestClient(main.app, client=LOCAL)                                             # 本机上的反向代理（Caddy）转发来的：带 X-Forwarded-For，也算「远程」
    assert login(proxied, "root", "123456").json()["must_change"] is False                   # 这次登录请求没带代理头：按本机直接打开算
    assert proxied.get("/api/watchlist").status_code == 200
    assert proxied.get("/api/watchlist", headers={"x-forwarded-for": "1.2.3.4"}).status_code == 403
    remote = device(main)
    r = login(remote)                                                                         # 同一账号再登录：把上面那个顶下线（一个账号只能一处登录）
    assert r.status_code == 200 and r.json()["must_change"] is True and r.json()["kicked"] is True
    blocked = remote.get("/api/watchlist")
    assert blocked.status_code == 403 and blocked.json()["code"] == "must_change"
    assert remote.get("/api/auth/me").json()["must_change"] is True                         # 这几个接口仍可用
    assert remote.post("/api/auth/password", json={"old": "123456", "new": "better-pass"}).status_code == 200
    assert remote.get("/api/watchlist").status_code == 200
    assert accounts.get_user(1)["default_password"] is False


def test_login_errors_do_not_reveal_whether_the_user_exists(main):
    c = device(main)
    a = login(c, "root", "wrong")
    b = login(c, "nobody", "wrong")
    assert a.status_code == b.status_code == 401 and a.json()["detail"] == b.json()["detail"] == "用户名或密码不对"
    assert login(c, "", "").status_code == 401


# ---------------------------------------------------------------- 防猜密码
def test_five_wrong_passwords_lock_that_user_on_that_ip_for_15_minutes(main):
    c = device(main)
    for _ in range(5):
        assert login(c, "root", "bad").status_code == 401
    r = login(c, "root", "123456")                                                           # 锁了：密码对也进不去
    assert r.status_code == 429 and r.json()["code"] == "locked" and "分钟" in r.json()["detail"]
    other_ip = device(main, ("8.8.4.4", 1))
    assert login(other_ip).status_code == 200                                                # 别的 IP 不受影响（别人没法靠乱试把管理员锁在外面）
    con = sqlite3.connect(accounts._path())
    con.execute("UPDATE login_fail SET ts = ts - 1000")                                      # 过了 15 分钟
    con.commit()
    con.close()
    assert login(c).status_code == 200


def test_one_ip_cannot_try_many_usernames(main):
    c = device(main)
    for i in range(30):
        login(c, f"user{i}", "bad")
    r = login(c, "root", "123456")
    assert r.status_code == 429


def test_the_old_access_token_header_means_nothing(main):
    c = device(main)
    assert c.get("/api/watchlist", headers={"X-Token": "123456"}).status_code == 401
    assert c.get("/api/watchlist?token=123456").status_code == 401


# ---------------------------------------------------------------- 账号管理
def test_admin_creates_user_who_must_set_own_password_and_admin_never_sees_passwords(main, admin):
    assert admin.post("/api/accounts", json={"username": "alice", "password": "temp123"}).status_code == 200
    listing = admin.get("/api/accounts")
    blob = listing.text
    assert "temp123" not in blob and "pw_hash" not in blob and "scrypt" not in blob           # 接口里没有任何密码相关内容
    c = device(main)
    r = login(c, "alice", "temp123")
    assert r.json()["must_change"] is True and r.json()["user"]["role"] == "user"
    assert c.get("/api/positions").status_code == 403                                         # 改密码之前什么都不能用
    assert c.post("/api/auth/password", json={"old": "temp123", "new": "short"}).status_code == 400
    assert c.post("/api/auth/password", json={"old": "nope", "new": "alice-pass"}).json()["code"] == "bad_old"
    assert c.post("/api/auth/password", json={"old": "temp123", "new": "temp123"}).json()["code"] == "same"
    assert c.post("/api/auth/password", json={"old": "temp123", "new": "alice-pass"}).status_code == 200
    assert c.get("/api/positions").status_code == 200
    assert login(device(main), "alice", "temp123").status_code == 401                         # 临时密码作废：管理员知道的那个密码没用了
    assert login(device(main), "alice", "alice-pass").status_code == 200


def test_username_rules(main, admin):
    assert admin.post("/api/accounts", json={"username": "a", "password": "abcdef"}).status_code == 400
    assert admin.post("/api/accounts", json={"username": "bad name!", "password": "abcdef"}).status_code == 400
    assert admin.post("/api/accounts", json={"username": "小王", "password": "abcdef"}).status_code == 200
    assert admin.post("/api/accounts", json={"username": "ROOT", "password": "abcdef"}).status_code == 409        # 不分大小写，不能和管理员重名
    assert admin.post("/api/accounts", json={"username": "小王", "password": "abcdef"}).status_code == 409
    uid = [u["id"] for u in admin.get("/api/accounts").json()["users"] if u["username"] == "小王"][0]
    assert admin.put(f"/api/accounts/{uid}", json={"username": "xiaowang"}).json()["user"]["username"] == "xiaowang"
    assert admin.post("/api/accounts", json={"username": "x1", "password": "12345"}).status_code == 400            # 密码至少 6 位
    assert login(device(main), "XiaoWang", "abcdef").status_code == 200                                          # 登录也不分大小写


def test_each_account_has_its_own_watchlist_positions_and_account(main, admin):
    sym = admin.get("/api/search", params={"q": "演示股票00"}).json()["items"][0]["symbol"]
    assert len(admin.put("/api/watchlist", json={"add": [{"symbol": sym}]}).json()["items"]) == 1
    admin.put("/api/account", json={"equity": 500000, "cash": 500000, "risk_per_trade": 0.005})
    alice = new_user(admin)
    assert alice.get("/api/watchlist").json()["items"] == []                                  # 看不到管理员的自选
    assert alice.get("/api/account").json()["account"].get("equity") in (None, 0)               # 也看不到管理员的资金
    alice.put("/api/watchlist", json={"add": [{"symbol": sym, "note": "alice 的"}]})
    alice.put("/api/account", json={"equity": 1000, "cash": 1000, "risk_per_trade": 0.01})
    assert alice.post("/api/trades", json={"symbol": sym, "date": "2026-09-01", "side": "buy", "price": 10, "qty": 100, "initial_stop": 9}).status_code == 200
    assert len(alice.get("/api/positions").json()["items"]) == 1
    assert admin.get("/api/positions").json()["items"] == []                                  # 管理员没有这笔持仓
    assert admin.get("/api/account").json()["account"]["equity"] == 500000
    assert admin.get("/api/watchlist").json()["items"][0].get("note") != "alice 的"
    uid = [u["id"] for u in admin.get("/api/accounts").json()["users"] if u["username"] == "alice"][0]
    assert (settings.data_dir() / "users" / str(uid) / "portfolio.db").exists() and (settings.data_dir() / "portfolio.db").exists()


def test_ordinary_account_cannot_use_admin_endpoints_but_can_read_and_edit_its_own(main, admin):
    alice = new_user(admin)
    for method, path, body in [("put", "/api/settings", {"execution": {"slippage": 0.002}}), ("get", "/api/settings", None), ("post", "/api/jobs/daily", {}),
                               ("post", "/api/scan/run", None), ("get", "/api/accounts", None), ("post", "/api/accounts", {"username": "evil", "password": "abcdef"}),
                               ("get", "/api/backups", None), ("post", "/api/restart", {}), ("post", "/api/upgrade/start", {}), ("get", "/api/upgrade/check", None),
                               ("post", "/api/backtest", {"kind": "x", "params": {}}), ("put", "/api/notify", {"enabled": True}), ("get", "/api/notify", None),
                               ("post", "/api/events/refresh", {}), ("get", "/api/storage", None), ("post", "/api/storage/clean", {"key": "demo"}),
                               ("get", "/api/data/health", None), ("delete", "/api/accounts/1", None), ("put", "/api/accounts/1/password", {"password": "hacked1"})]:
        r = getattr(alice, method)(path, **({"json": body} if body is not None else {}))
        assert r.status_code == 403 and r.json()["code"] == "forbidden", (method, path, r.status_code)
    assert alice.get("/api/scan").status_code == 200 and alice.get("/api/search", params={"q": "演示"}).status_code == 200      # 行情、选股结果能看
    j = alice.get("/api/jobs").json()
    assert j["has_data"] and "backup" not in j and "logs" not in j and "throttle" not in j                              # 备份目录、任务日志是管理员的事
    assert admin.get("/api/jobs").json()["backup"] is not None
    assert admin.get("/api/settings").status_code == 200 and admin.get("/api/accounts").status_code == 200
    assert login(device(main), "root", "hacked1").status_code == 401                                                    # 管理员密码没被改掉


def test_pause_blocks_login_and_logs_out_but_keeps_data_and_resume_restores(main, admin):
    sym = admin.get("/api/search", params={"q": "演示股票00"}).json()["items"][0]["symbol"]
    alice = new_user(admin)
    alice.put("/api/watchlist", json={"add": [{"symbol": sym}]})
    uid = [u["id"] for u in admin.get("/api/accounts").json()["users"] if u["username"] == "alice"][0]
    assert admin.put(f"/api/accounts/{uid}", json={"status": "paused"}).json()["user"]["status"] == "paused"
    r = alice.get("/api/watchlist")
    assert r.status_code == 401 and r.json()["code"] == "paused"                              # 已登录的也立刻下线
    p = login(device(main), "alice", "alice-pass")
    assert p.status_code == 403 and p.json()["code"] == "paused"
    assert login(device(main), "alice", "wrong").json()["code"] == "bad_login"               # 密码不对时不透露账号被暂停
    assert (settings.data_dir() / "users" / str(uid) / "portfolio.db").exists()               # 数据还在
    admin.put(f"/api/accounts/{uid}", json={"status": "active"})
    again = device(main)
    assert login(again, "alice", "alice-pass").status_code == 200 and len(again.get("/api/watchlist").json()["items"]) == 1


def test_delete_removes_the_account_and_all_its_data_but_never_the_admin(main, admin):
    alice = new_user(admin)
    alice.put("/api/account", json={"equity": 1000, "cash": 1000, "risk_per_trade": 0.01})
    uid = [u["id"] for u in admin.get("/api/accounts").json()["users"] if u["username"] == "alice"][0]
    udir = settings.data_dir() / "users" / str(uid)
    assert udir.exists()
    d = admin.delete(f"/api/accounts/{uid}").json()
    assert d["ok"] and d["data_removed"] is True and not udir.exists()
    assert alice.get("/api/watchlist").status_code == 401
    assert login(device(main), "alice", "alice-pass").status_code == 401
    assert [u["username"] for u in admin.get("/api/accounts").json()["users"]] == ["root"]
    assert admin.delete("/api/accounts/1").status_code == 400                                # 管理员不能删自己
    assert admin.put("/api/accounts/1", json={"status": "paused"}).status_code == 400        # 也不能暂停
    assert admin.delete("/api/accounts/999").status_code == 404
    assert admin.get("/api/watchlist").status_code == 200


def test_admin_reset_password_kicks_the_user_and_forces_a_new_password(main, admin):
    alice = new_user(admin)
    uid = [u["id"] for u in admin.get("/api/accounts").json()["users"] if u["username"] == "alice"][0]
    assert admin.put(f"/api/accounts/{uid}/password", json={"password": "short"}).status_code == 400
    assert admin.put(f"/api/accounts/{uid}/password", json={"password": "reset-123"}).status_code == 200
    r = alice.get("/api/watchlist")
    assert r.status_code == 401 and r.json()["code"] == "reset"
    assert login(device(main), "alice", "alice-pass").status_code == 401
    c = device(main)
    assert login(c, "alice", "reset-123").json()["must_change"] is True
    assert c.get("/api/watchlist").status_code == 403
    assert c.post("/api/auth/password", json={"old": "reset-123", "new": "mine-again"}).status_code == 200
    assert c.get("/api/watchlist").status_code == 200


# ---------------------------------------------------------------- 一个账号只能一处登录
def test_second_login_kicks_the_first(main, admin):
    phone = new_user(admin)
    laptop = device(main)
    r = login(laptop, "alice", "alice-pass")
    assert r.status_code == 200 and r.json()["kicked"] is True                               # 告诉新登录的人：顶掉了另一处
    gone = phone.get("/api/watchlist")
    assert gone.status_code == 401 and gone.json()["code"] == "kicked" and "另一台设备" in gone.json()["detail"]
    assert laptop.get("/api/watchlist").status_code == 200
    log = admin.get("/api/accounts").json()["log"]
    assert any(x["username"] == "alice" and x["ok"] == 1 and "顶掉" in x["note"] for x in log)
    again = device(main)
    assert login(again, "alice", "alice-pass").json()["kicked"] is True and laptop.get("/api/watchlist").status_code == 401


def test_logout_ends_the_session_and_login_log_records_failures(main):
    c = device(main, ("9.9.9.9", 1))
    login(c, "root", "bad")
    assert login(c, "root", "123456").json()["kicked"] is False
    c.post("/api/auth/password", json={"old": "123456", "new": "abc-1234"})
    assert c.post("/api/auth/logout").status_code == 200
    r = c.get("/api/auth/me")
    assert r.status_code == 401
    admin = device(main, LOCAL)
    assert login(admin, "root", "abc-1234").status_code == 200
    log = admin.get("/api/accounts").json()["log"]
    assert any(x["ip"] == "9.9.9.9" and x["ok"] == 0 for x in log) and any(x["ip"] == "9.9.9.9" and x["ok"] == 1 for x in log)


def test_unknown_or_expired_cookie_asks_for_login(main):
    c = device(main)
    c.cookies.set(accounts.COOKIE, "not-a-real-session")
    r = c.get("/api/watchlist")
    assert r.status_code == 401 and r.json()["code"] == "expired"
    assert device(main).get("/api/auth/me").json()["code"] == "none"


# ---------------------------------------------------------------- Cookie 与代理
def test_cookie_is_httponly_lax_and_secure_only_over_https(main):
    r = login(device(main))
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=lax" in sc and "secure" not in sc and "path=/" in sc
    r2 = device(main).post("/api/auth/login", json={"username": "root", "password": "123456"}, headers={"x-forwarded-proto": "https"})
    assert "secure" in r2.headers["set-cookie"].lower()
    assert "123456" not in r.text                                                             # 响应里没有密码


def test_client_ip_trusts_forwarded_header_only_from_a_local_proxy():
    mk = lambda host, xff=None: SimpleNamespace(client=SimpleNamespace(host=host), headers={"x-forwarded-for": xff} if xff else {})   # noqa: E731
    assert accounts.client_ip(mk("8.8.8.8", "1.1.1.1")) == "8.8.8.8"                          # 公网来的：自己报的 X-Forwarded-For 不信
    assert accounts.client_ip(mk("127.0.0.1", "6.6.6.6, 5.5.5.5")) == "5.5.5.5"               # 本机的代理：取代理追加的最后一个
    assert accounts.client_ip(mk("127.0.0.1")) == "127.0.0.1"


def test_stock_auth_off_only_works_when_listening_on_this_machine(demo_env, monkeypatch):
    monkeypatch.setenv("STOCK_AUTH", "off")
    assert accounts.auth_required() is False
    monkeypatch.setitem(settings.cfg()["server"], "host", "0.0.0.0")
    assert accounts.auth_required() is True                                                   # 对局域网开放时，这个开关无效
    monkeypatch.delenv("STOCK_AUTH")
    monkeypatch.setitem(settings.cfg()["server"], "host", "127.0.0.1")
    assert accounts.auth_required() is True


def test_passwords_are_stored_as_salted_hashes(main, admin):
    con = sqlite3.connect(accounts._path())
    hashes = [r[0] for r in con.execute("SELECT pw_hash FROM users")]
    con.close()
    assert hashes and all(h.startswith(("scrypt$", "pbkdf2$")) and "123456" not in h for h in hashes)
    assert accounts.hash_password("same") != accounts.hash_password("same")                  # 加盐：同一个密码每次哈希不同
    assert accounts.verify_password("same", accounts.hash_password("same")) and not accounts.verify_password("other", accounts.hash_password("same"))


# ---------------------------------------------------------------- 提醒邮箱
def test_ordinary_account_sets_only_its_own_reminder_address(main, admin):
    alice = new_user(admin)
    assert alice.put("/api/me/notify", json={"notify_to": "not-an-email"}).status_code == 400
    assert alice.put("/api/me/notify", json={"notify_to": "alice@qq.com"}).json()["notify_to"] == "alice@qq.com"
    assert alice.get("/api/me/notify").json()["notify_to"] == "alice@qq.com"
    assert admin.put("/api/me/notify", json={"notify_to": "root@qq.com"}).status_code == 400            # 管理员的邮箱在「设置 → 邮件提醒」
    assert [u["username"] for u in accounts.notify_users()] == ["alice"]
    uid = [u["id"] for u in admin.get("/api/accounts").json()["users"] if u["username"] == "alice"][0]
    admin.put(f"/api/accounts/{uid}", json={"status": "paused"})
    assert accounts.notify_users() == []                                                      # 暂停的不发


def test_after_scan_sends_each_account_its_own_positions_mail(main, admin, monkeypatch):
    from server import portfolio
    alice = new_user(admin)
    alice.put("/api/me/notify", json={"notify_to": "alice@qq.com"})
    uid = [u["id"] for u in admin.get("/api/accounts").json()["users"] if u["username"] == "alice"][0]
    mails = []
    monkeypatch.setattr(notify, "send_mail", lambda s, b, c=None: mails.append((c["recipients"], b)) or (True, "ok"))
    monkeypatch.setitem(settings.base_cfg(), "notify", {"enabled": True, "sender": "a@qq.com", "password": "x", "host": "smtp.qq.com", "port": 465, "security": "ssl",
                                                        "recipients": "root@qq.com", "events": {"panic": False, "etf": False, "positions": True}})
    mine = lambda s: {"positions": [{"level": "must", "symbol": "sh.600000", "name": s, "action": "止损"}]}    # noqa: E731
    monkeypatch.setattr(portfolio, "health", lambda market, day=None, save=True: mine("ALICE-STOCK") if settings.current_user_id() == uid else mine("OTHER"))
    from server import market_calendar as mc
    monkeypatch.setattr("server.market_view.build", lambda market: {"status": "no_data"})
    with db.market_db("CN") as c:
        day = mc.last_closed_trading_day(c, "CN")
    out = notify.after_scan("CN", day, {"summary": {}}, mine("ROOT-STOCK"))
    assert out["positions"] == "已发送" and out["账号 alice"]["positions"] == "已发送"
    assert [m[0] for m in mails] == ["root@qq.com", "alice@qq.com"]
    assert "ROOT-STOCK" in mails[0][1] and "ALICE-STOCK" not in mails[0][1] and "ALICE-STOCK" in mails[1][1] and "ROOT-STOCK" not in mails[1][1]
    mails.clear()
    out2 = notify.after_scan("CN", day, {"summary": {}}, mine("ROOT-STOCK"))                    # 同一天不重复发
    assert out2["positions"] == "已发过" and out2["账号 alice"]["positions"] == "已发过" and mails == []


# ---------------------------------------------------------------- 备份
def test_backup_and_restore_include_the_account_db_and_every_accounts_portfolio(main, admin):
    from server import backup
    sym = admin.get("/api/search", params={"q": "演示股票00"}).json()["items"][0]["symbol"]
    admin.put("/api/watchlist", json={"add": [{"symbol": sym}]})                              # 管理员的个人库（data/portfolio.db）有内容才会被备份
    alice = new_user(admin)
    alice.put("/api/watchlist", json={"add": [{"symbol": sym}]})
    uid = [u["id"] for u in admin.get("/api/accounts").json()["users"] if u["username"] == "alice"][0]
    info = backup.run_backup(full=False)
    assert info["files"]["accounts.db"] == "ok" and info["files"][f"users_{uid}_portfolio.db"]["watchlist"] == 1
    upath = settings.data_dir() / "users" / str(uid) / "portfolio.db"
    upath.unlink()
    out = backup.restore(__import__("datetime").date.today().isoformat(), "portfolio")
    assert out[f"users_{uid}_portfolio.db"]["watchlist"] == 1 and upath.exists() and "accounts.db" in out
    assert any(i["key"] in ("accounts", "users") for i in backup.storage_report()["items"])
    assert json.loads(json.dumps(accounts.list_users()))                                      # 账号库恢复后仍能读
