"""港股（第 2 步）：指数日线（东财）解析 / 入库 / 增量、均线位置、自动更新的时间判断、接口。全部用假的网络接口，不联网。"""
import json
import warnings
from datetime import datetime, timedelta

import pandas as pd
import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

from server import db, hk, valuation  # noqa: E402
from tests.test_valuation import cur_of, fake_http, hk_rows  # noqa: E402


def kline(n=300, start="2025-01-01", step=1.0, base=100.0, flat_head=0):
    """东财格式的假 K 线：日期, 开, 收, 高, 低, 成交量, 成交额。前 flat_head 条是「回溯数据」（高 = 低、成交量 0）。"""
    out = []
    d0 = datetime.fromisoformat(start)
    for i in range(n):
        d = (d0 + timedelta(days=i)).date().isoformat()
        c = base + step * i
        if i < flat_head:
            out.append(f"{d},{c},{c},{c},{c},0,0.00")
        else:
            out.append(f"{d},{c - 0.5},{c},{c + 1},{c - 1},1000,{c * 1000}")
    return out


def fake_get(by_secid, calls=None):
    def get(params, timeout=25):
        if calls is not None:
            calls.append(dict(params))
        return {"klines": by_secid.get(params["secid"], [])}
    return get


# ---------------------------------------------------------------- 解析
def test_fetch_bars_field_order_and_flat_rows_dropped(monkeypatch):
    monkeypatch.setattr(hk, "_get", fake_get({"124.HSTECH": kline(50, flat_head=10)}))
    df = hk.fetch_bars("124.HSTECH", "20150101")
    assert len(df) == 40 and df["date"].is_monotonic_increasing                            # 回溯的 10 条（没有真实成交）丢掉
    r = df.iloc[0]                                                                         # 第 11 天：close = 110，open = 109.5，high = 111，low = 109
    assert (r.close, r.open, r.high, r.low) == (110.0, 109.5, 111.0, 109.0) and r.volume == 1000


def test_fetch_bars_empty(monkeypatch):
    monkeypatch.setattr(hk, "_get", fake_get({}))
    assert hk.fetch_bars("100.HSI", "20150101").empty


def test_http_failure_on_every_node_raises_and_one_working_node_is_enough(monkeypatch):
    import requests
    seen = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"klines": ["2026-11-06,1,2,3,0.5,10,20"]}}

    def flaky(url, **k):
        seen.append(url)
        if "82.push2his" not in url:
            raise OSError("down")                                                          # 第一个节点挂了，第二个能用
        return Resp()
    monkeypatch.setattr(requests, "get", flaky)
    assert hk._get({"secid": "100.HSI"})["klines"] and len(seen) == 2

    def dead(url, **k):
        raise OSError("down")
    monkeypatch.setattr(requests, "get", dead)
    with pytest.raises(RuntimeError):
        hk._get({"secid": "100.HSI"})                                                       # 两个节点都失败才报错


# ---------------------------------------------------------------- 入库与增量
def test_refresh_indices_full_then_incremental(demo_env, monkeypatch):
    calls = []
    monkeypatch.setattr(hk, "_get", fake_get({s: kline(300) for s in hk.SECIDS.values()}, calls))
    with db.market_db("HK") as c:
        r = hk.refresh_indices(c)
        assert not r["errors"] and set(r["rows"]) == set(hk.SECIDS) and all(v == 300 for v in r["rows"].values())
        assert {p["beg"] for p in calls} == {"20150101"}                                    # 第一次：全量
        calls.clear()
        r = hk.refresh_indices(c)
        last = c.execute("SELECT MAX(date) FROM index_bar WHERE symbol='HK.HSI'").fetchone()[0]
        assert {p["beg"] for p in calls} == {(datetime.fromisoformat(last) - timedelta(days=15)).strftime("%Y%m%d")}   # 之后：最后一天往前 15 天
        assert c.execute("SELECT COUNT(*) FROM index_bar WHERE symbol='HK.HSI'").fetchone()[0] == 300                  # 重叠的日子是覆盖，不重复


def test_one_failing_index_does_not_stop_the_others(demo_env, monkeypatch):
    monkeypatch.setattr(hk, "_get", fake_get({"100.HSI": kline(100), "100.HSCEI": kline(100)}))                      # 恒生科技没有数据
    with db.market_db("HK") as c:
        r = hk.refresh_indices(c)
        assert r["rows"]["HK.HSI"] == 100 and r["rows"]["HK.HSTECH"] == 0 and not r["errors"]
    monkeypatch.setattr(hk, "fetch_bars", lambda secid, beg: (_ for _ in ()).throw(RuntimeError("断网")) if secid == "100.HSI" else pd.DataFrame())
    with db.market_db("HK") as c:
        r = hk.refresh_indices(c)
        assert len(r["errors"]) == 1 and "HK.HSI" in r["errors"][0]


# ---------------------------------------------------------------- 均线位置
def frame(step, n=300, base=1000.0):
    rows = [{"date": (datetime(2025, 1, 1) + timedelta(days=i)).date().isoformat(), "open": base + step * i, "high": base + step * i + 1, "low": base + step * i - 1, "close": base + step * i} for i in range(n)]
    return pd.DataFrame(rows)


def test_trend_labels_follow_how_many_moving_averages_price_is_above():
    up = hk.analyze("HK.HSI", "恒生指数", frame(+2.0))
    assert up["trend"] == "强势" and all(m["above"] for m in up["mas"]) and [m["n"] for m in up["mas"]] == [20, 60, 120, 200, 250]
    down = hk.analyze("HK.HSI", "恒生指数", frame(-2.0))
    assert down["trend"] == "弱势" and not any(m["above"] for m in down["mas"])
    closes = [1000.0 + 2.0 * k for k in range(280)] + [1558.0 - 3.0 * k for k in range(1, 11)]          # 涨了 280 天，最近 10 天小幅回落：只跌破 20 日线
    dip = hk.analyze("HK.HSI", "恒生指数", pd.DataFrame({"date": [(datetime(2025, 1, 1) + timedelta(days=k)).date().isoformat() for k in range(len(closes))],
                                                      "open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes], "close": closes}))
    above = {m["n"]: m["above"] for m in dip["mas"]}
    assert above[20] is False and above[60] and above[120] and above[200] and dip["trend"] == "偏强"
    assert up["history"][-1]["ma200"] is not None and len(up["history"]) == 300 and up["dd_52w"] == pytest.approx(up["close"] / up["hi_52w"] - 1, abs=1e-3)


def test_short_history_says_so_instead_of_inventing_averages():
    r = hk.analyze("HK.HSTECH", "恒生科技", frame(+1.0, 150))
    assert r["trend"] == "数据不足" and [m["n"] for m in r["mas"]] == [20, 60, 120]
    assert hk.analyze("HK.HSTECH", "恒生科技", frame(+1.0, 20)) is None


# ---------------------------------------------------------------- 自动更新的时间判断
def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=hk.HKT)


def test_auto_due_rules(demo_env):
    with db.market_db("HK") as c:
        assert hk.auto_due(c, at(2026, 11, 9, 16, 50)) is True                               # 周一 16:50、今天还没做
        assert hk.auto_due(c, at(2026, 11, 9, 16, 30)) is False                              # 还没到 16:45
        assert hk.auto_due(c, at(2026, 11, 7, 18, 0)) is False and hk.auto_due(c, at(2026, 11, 8, 18, 0)) is False   # 周六、周日
        db.set_meta(c, hk.META, json.dumps({"day": "2026-11-09", "ok": True, "at": datetime.now().isoformat(timespec="seconds")}))
        assert hk.auto_due(c, at(2026, 11, 9, 20, 0)) is False                               # 今天已经成功做过
        db.set_meta(c, hk.META, json.dumps({"day": "2026-11-09", "ok": False, "at": datetime.now().isoformat(timespec="seconds")}))
        assert hk.auto_due(c, at(2026, 11, 9, 20, 0)) is False                               # 刚失败：3 小时内不反复试
        db.set_meta(c, hk.META, json.dumps({"day": "2026-11-09", "ok": False, "at": (datetime.now() - timedelta(hours=4)).isoformat(timespec="seconds")}))
        assert hk.auto_due(c, at(2026, 11, 9, 20, 0)) is True                                # 失败超过 3 小时：再试
        db.set_meta(c, hk.META, json.dumps({"day": "2026-11-06", "ok": True, "at": "2026-11-06T17:00:00"}))
        assert hk.auto_due(c, at(2026, 11, 9, 17, 0)) is True                                # 上次是上周五：今天要做


def test_auto_update_default_is_off_for_hk_like_other_non_cn_markets():
    from server import settings
    assert settings.auto_update_enabled("HK") is False and settings.auto_update_enabled("CN") is True


# ---------------------------------------------------------------- 刷新 + 接口
@pytest.fixture
def client(demo_env, monkeypatch):
    from server import main
    monkeypatch.setattr(hk, "_get", fake_get({s: kline(300) for s in hk.SECIDS.values()}))
    monkeypatch.setattr(valuation, "_http", fake_http(hk_rows(), {c: cur_of(9.0) for c in valuation.CODES_BY_MARKET["HK"].values()}))
    monkeypatch.setattr(hk, "refresh_async", lambda source="page": True)
    monkeypatch.setattr(valuation, "refresh_async", lambda m="CN": True)
    return TestClient(main.app)


def test_refresh_all_writes_record_and_both_kinds_of_data(client):
    with db.market_db("HK") as c:
        info = hk.refresh_all(c, "cli")
        assert info["ok"] and info["day"] == hk.today_hkt() and info["source"] == "cli" and not info["errors"]
        assert db.get_meta(c, "valuation_hk") and hk.last_refresh(c)["ok"]


def test_api_overview_loading_then_ok_and_status_and_manual_refresh(client):
    d = client.get("/api/hk/overview").json()
    assert d["market"] == "HK" and d["status"] == "loading"                                    # 第一次没有数据：后台取
    r = client.post("/api/hk/refresh").json()
    assert r["ok"] and "已更新" in r["message"]
    d = client.get("/api/hk/overview").json()
    assert d["status"] == "ok" and [i["name"] for i in d["indices"]] == ["恒生指数", "国企指数", "恒生科技"]
    i0 = d["indices"][0]
    assert i0["trend"] == "强势" and i0["valuation"]["pe"] == 9.0 and i0["etf"] and len(i0["history"]) == 300 and "不是买卖信号" in d["note"]
    st = client.get("/api/hk/status").json()
    assert st["has_data"] and st["auto_update"] is False and [i["rows"] for i in st["indices"]] == [300, 300, 300] and st["valuation_at"] and st["last"]["ok"]
    assert (client.get("/api/market/view", params={"market": "HK"}).status_code, client.get("/api/jobs", params={"market": "HK"}).status_code) == (400, 400)


def test_manual_refresh_reports_failures_honestly(client, monkeypatch):
    monkeypatch.setattr(hk, "fetch_bars", lambda secid, beg: (_ for _ in ()).throw(RuntimeError("断网")))
    r = client.post("/api/hk/refresh").json()
    assert r["ok"] is False and "没有全部成功" in r["message"]


def test_scheduler_tick_updates_hk_only_when_switched_on_and_due(demo_env, monkeypatch):
    from server import jobs, market_calendar as mc, settings
    called = []
    monkeypatch.setattr(hk, "refresh_async", lambda source="page": called.append(source) or True)
    monkeypatch.setattr(mc, "last_closed_trading_day", lambda conn, market, now=None: None)         # A 股 / 美股这一轮什么都不做
    monkeypatch.setattr(hk, "auto_due", lambda c, now=None: True)
    mgr = jobs.JobManager()
    mgr.tick()
    assert called == []                                                                            # 默认关着：不更新
    settings.load_config()["jobs"]["auto_update"]["HK"] = True
    mgr.tick()
    assert called == ["auto"]
    monkeypatch.setattr(hk, "auto_due", lambda c, now=None: False)
    mgr.tick()
    assert called == ["auto"]                                                                      # 还没到时间 / 今天做过了：不再触发
