"""港股个股行情库（第 3 步）：名单（东财）/ 日线（雅虎批量）/ 每手股数 / 任务 / K 线 / 搜索 / 接口。全部用假的网络接口，不联网。"""
import time
import warnings

import numpy as np
import pandas as pd
import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

from server import db, hk, hk_stocks  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_job():
    hk_stocks._job.update({"running": False, "kind": None, "total": 0, "done": 0, "message": "", "error": None, "started": None, "finished": None, "stop": False, "errors": []})
    yield


def rows(n, start=700, amount0=9e9):
    """东财列表的假数据：按成交额从大到小。"""
    return [{"f12": f"{start + i:05d}", "f14": f"测试股{i}", "f2": 10.0 + i, "f3": 1.5, "f5": 1e6, "f6": amount0 - i * 1e6, "f21": 1e10} for i in range(n)]


def fake_json(total=250, lots=None, fail_page=None, calls=None):
    def get(path, params, timeout=40, tries=2):
        if calls is not None:
            calls.append((path, dict(params)))
        if path.endswith("/clist/get"):
            p = params["pn"]
            if fail_page == p:
                raise RuntimeError("断网")
            return {"total": total, "diff": rows(total)[(p - 1) * 100:p * 100]}
        code = params["secid"].split(".")[1]
        return {"f602": (lots or {}).get(code, 100)}
    return get


def fake_download(days=120, missing=(), fail_calls=(), calls=None):
    def dl(tickers, start, period):
        if calls is not None:
            calls.append((list(tickers), start, period))
        if len(calls or []) in fail_calls:
            raise RuntimeError("雅虎限流")
        idx = pd.bdate_range(end="2026-11-06", periods=days)
        frames = {}
        for t in tickers:
            if t in missing:
                continue
            close = pd.Series(100.0 + np.arange(days) * 0.1, index=idx)
            adj = close * np.where(np.arange(days) < days // 2, 0.9, 1.0)               # 前一半日子有分红复权：复权价是收盘价的 90%
            vol = pd.Series(1000.0, index=idx)
            vol.iloc[3] = 0.0                                                          # 一天停牌
            f = pd.DataFrame({"Open": close - 0.2, "High": close + 0.5, "Low": close - 0.5, "Close": close, "Adj Close": adj, "Volume": vol})
            if start:
                f = f[f.index >= pd.Timestamp(start)]
            frames[t] = f
        return pd.concat(frames, axis=1) if frames else pd.DataFrame()
    return dl


# ---------------------------------------------------------------- 代码与名单
def test_yahoo_ticker_mapping():
    assert hk_stocks.yahoo_ticker("hk.00700") == "0700.HK" and hk_stocks.yahoo_ticker("hk.09988") == "9988.HK" and hk_stocks.yahoo_ticker("hk.80737") == "80737.HK"
    assert hk_stocks.sym_of("700") == "hk.00700" and hk_stocks.code_of("hk.00700") == "00700"


def test_fetch_list_pages_filters_dashes_and_respects_top(monkeypatch):
    calls = []
    data = rows(250)
    data[1]["f2"] = "-"                                                                # 停牌：价格 "-"
    data[2]["f6"] = "-"
    monkeypatch.setattr(hk_stocks, "_get_json", lambda path, params, timeout=40: (calls.append(params["pn"]) or {"diff": data[(params["pn"] - 1) * 100:params["pn"] * 100]}))
    out = hk_stocks.fetch_list(top=250)
    assert len(out) == 248 and out[0]["symbol"] == "hk.00700" and out[0]["amount"] == 9e9 and calls == [1, 2, 3]
    assert len(hk_stocks.fetch_list(top=120)) == 120                                   # 取 2 页（198 只有效）再截断到 120
    assert len(hk_stocks.fetch_list(top=100)) == 98                                    # 只取 1 页（这一页里有 2 只停牌的）


def test_fetch_list_keeps_pages_already_fetched_when_a_later_page_fails(monkeypatch):
    monkeypatch.setattr(hk_stocks, "_get_json", fake_json(total=300, fail_page=2))
    assert len(hk_stocks.fetch_list(top=300)) == 100


def test_refresh_universe_marks_dropped_stocks_and_keeps_snapshot(demo_env, monkeypatch):
    monkeypatch.setattr(hk_stocks, "_get_json", fake_json(total=150))
    with db.market_db("HK") as c:
        r = hk_stocks.refresh_universe(c, top=150)
        assert r["n"] == 150 and c.execute("SELECT COUNT(*) FROM securities WHERE in_l1=1").fetchone()[0] == 150
        monkeypatch.setattr(hk_stocks, "_get_json", fake_json(total=60))
        hk_stocks.refresh_universe(c, top=60)                                           # 名单缩短：掉出去的不在池里，但记录还在
        assert c.execute("SELECT COUNT(*) FROM securities WHERE in_l1=1").fetchone()[0] == 60
        assert c.execute("SELECT COUNT(*) FROM securities").fetchone()[0] == 150
        assert hk_stocks.universe_meta(c)["n"] == 60 and hk_stocks.universe_meta(c)["items"][0]["symbol"] == "hk.00700"
        monkeypatch.setattr(hk_stocks, "_get_json", lambda *a, **k: {"diff": []})
        with pytest.raises(RuntimeError):
            hk_stocks.refresh_universe(c, top=60)                                       # 取不到名单：报错，旧名单原样保留
        assert c.execute("SELECT COUNT(*) FROM securities WHERE in_l1=1").fetchone()[0] == 60


# ---------------------------------------------------------------- 日线
def test_download_bars_stores_prices_adjustment_amount_and_suspension(demo_env, monkeypatch):
    monkeypatch.setattr(hk_stocks, "_download", fake_download(days=100, missing={"0702.HK"}))
    with db.market_db("HK") as c:
        r = hk_stocks.download_bars(c, ["hk.00700", "hk.00701", "hk.00702"])
        assert r == {"ok": 2, "empty": 1, "failed": []}
        b = c.execute("SELECT * FROM daily_bar WHERE symbol='hk.00700' ORDER BY date").fetchall()
        assert len(b) == 100 and b[-1]["adj_factor"] == pytest.approx(1.0) and b[0]["adj_factor"] == pytest.approx(0.9)
        assert b[-1]["amount"] == pytest.approx(b[-1]["close"] * 1000) and b[3]["trade_status"] == 0 and b[4]["trade_status"] == 1
        assert c.execute("SELECT status FROM fetch_state WHERE symbol='hk.00700' AND task='bars_hk'").fetchone()[0] == "ok"
        assert not c.execute("SELECT 1 FROM daily_bar WHERE symbol='hk.00702'").fetchone()


def test_one_failed_batch_does_not_stop_the_others(demo_env, monkeypatch):
    calls = []
    monkeypatch.setattr(hk_stocks, "BATCH", 2)
    monkeypatch.setattr(hk_stocks, "_download", fake_download(fail_calls={1}, calls=calls))
    syms = [f"hk.{700 + i:05d}" for i in range(5)]
    with db.market_db("HK") as c:
        r = hk_stocks.download_bars(c, syms)
    assert len(calls) == 3 and r["failed"] == syms[:2] and r["ok"] == 3                  # 第 1 批失败，后面两批照常


# ---------------------------------------------------------------- 任务
def wait_job(timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not hk_stocks._job["running"]:
            return hk_stocks.job_state()
        time.sleep(0.05)
    raise AssertionError("任务没有结束")


def test_init_job_builds_universe_and_bars_and_is_resumable(demo_env, monkeypatch):
    dl_calls = []
    monkeypatch.setattr(hk_stocks, "UNIVERSE_TOP", 120)
    monkeypatch.setattr(hk_stocks, "_get_json", fake_json(total=120))
    monkeypatch.setattr(hk_stocks, "_download", fake_download(days=60, calls=dl_calls))
    assert hk_stocks.start_job("init")["ok"]
    s = wait_job()
    assert s["error"] is None and s["message"] == "完成" and s["done"] == 120
    with db.market_db("HK") as c:
        st = hk_stocks.status(c)
        assert st["pool"] == 120 and st["with_bars"] == 120 and st["ready"] and st["bars_asof"] == "2026-11-06" and st["last_run"]["ok"] and st["last_run"]["kind"] == "init"
        c.execute("DELETE FROM daily_bar WHERE symbol IN ('hk.00700','hk.00701')")             # 模拟中断：两只没下完
        c.commit()
    dl_calls.clear()
    hk_stocks.start_job("init")
    wait_job()
    assert sum(len(t) for t, _, _ in dl_calls) == 2                                         # 续跑：只补缺的两只，不重下全部


def test_update_job_fetches_only_recent_days_and_only_one_job_at_a_time(demo_env, monkeypatch):
    dl_calls = []
    monkeypatch.setattr(hk_stocks, "_get_json", fake_json(total=100))
    fast = fake_download(days=60, calls=dl_calls)
    monkeypatch.setattr(hk_stocks, "_download", fast)
    with db.market_db("HK") as c:
        hk_stocks.refresh_universe(c, top=100)
        hk_stocks.download_bars(c, hk_stocks._symbols(c))
    dl_calls.clear()
    monkeypatch.setattr(hk_stocks, "_download", lambda *a, **k: (time.sleep(0.4), fast(*a, **k))[1])               # 让任务慢一点，才能确定地测到「正在跑」
    assert hk_stocks.start_job("update")["ok"]
    second = hk_stocks.start_job("update")
    assert second["ok"] is False and "正在更新" in second["message"]                                                 # 正在跑时不会再开第二个
    s = wait_job()
    assert s["error"] is None and dl_calls and all(start is not None and period is None for _, start, period in dl_calls)       # 增量：按起始日期取，不是 10 年
    with pytest.raises(ValueError):
        hk_stocks.start_job("nonsense")


def test_job_failure_is_reported_not_raised(demo_env, monkeypatch):
    monkeypatch.setattr(hk_stocks, "_get_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("断网")))
    hk_stocks.start_job("init")
    s = wait_job()
    assert s["error"] and "没有取到港股名单" in s["error"] and s["message"] == "失败" and not s["running"]


# ---------------------------------------------------------------- K 线、每手、搜索
@pytest.fixture
def loaded(demo_env, monkeypatch):
    monkeypatch.setattr(hk_stocks, "_get_json", fake_json(total=100, lots={"00700": 100, "00701": 400}))
    monkeypatch.setattr(hk_stocks, "_download", fake_download(days=260))
    with db.market_db("HK") as c:
        hk_stocks.refresh_universe(c, top=100)
        hk_stocks.download_bars(c, hk_stocks._symbols(c))
    return demo_env


def test_kline_is_forward_adjusted_and_ends_at_the_real_close(loaded):
    with db.market_db("HK") as c:
        k = hk_stocks.kline(c, "hk.00700")
        real = c.execute("SELECT close FROM daily_bar WHERE symbol='hk.00700' ORDER BY date DESC LIMIT 1").fetchone()[0]
    assert k["status"] == "ok" and k["name"] == "测试股0" and k["bars"][-1]["close"] == pytest.approx(real, abs=1e-2) and k["last"]["close"] == pytest.approx(real, abs=1e-2)
    assert k["bars"][0]["close"] < 100.0 * 0.95 and set(k["ma"]) == {"ma5", "ma10", "ma20", "ma50", "ma200"} and len(k["ma"]["ma200"]) < len(k["ma"]["ma5"])
    assert k["bars"][3]["time"] != k["bars"][2]["time"] and all(b["volume"] > 0 for b in k["bars"])             # 停牌日（成交量 0）不画
    assert hk_stocks.kline(db.market_conn("HK"), "hk.99999")["status"] == "no_data"


def test_lot_size_is_fetched_once_and_cached_and_failure_is_not_guessed(loaded, monkeypatch):
    calls = []
    monkeypatch.setattr(hk_stocks, "_get_json", fake_json(lots={"00701": 400}, calls=calls))
    with db.market_db("HK") as c:
        a = hk_stocks.lot_size(c, "hk.00701")
        b = hk_stocks.lot_size(c, "hk.00701")
        assert a["lot"] == 400 and a["cached"] is False and b["lot"] == 400 and b["cached"] is True and len(calls) == 1
        monkeypatch.setattr(hk_stocks, "_get_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("慢 / 断了")))
        bad = hk_stocks.lot_size(c, "hk.00750")
        assert bad["lot"] is None and "error" in bad and "hk.00750" not in hk_stocks._lots(c)               # 查不到不猜、不缓存


def test_search_by_code_name_pinyin_and_hot_list_when_empty(loaded):
    with db.market_db("HK") as c:
        assert [x["symbol"] for x in hk_stocks.search(c, "00705")][:1] == ["hk.00705"]
        assert hk_stocks.search(c, "测试股7")[0]["symbol"] == "hk.00707"
        hot = hk_stocks.search(c, "")
        assert hot[0]["symbol"] == "hk.00700" and hot[0]["amount"] > hot[1]["amount"] and len(hot) == 20          # 没输入：成交额最高的几只


# ---------------------------------------------------------------- 接口
def test_api_endpoints(loaded, monkeypatch):
    from server import main
    cl = TestClient(main.app)
    st = cl.get("/api/hk/stocks/status").json()
    assert st["market"] == "HK" and st["pool"] == 100 and st["with_bars"] == 100 and st["ready"] and st["job"]["running"] is False
    assert cl.get("/api/hk/stocks/search", params={"q": "00705"}).json()["items"][0]["symbol"] == "hk.00705"
    k = cl.get("/api/hk/stocks/kline", params={"symbol": "hk.00700"}).json()
    assert k["status"] == "ok" and k["bars"] and k["ma"]["ma20"] and k["last"]["amount_20d"]
    assert cl.get("/api/hk/stocks/kline", params={"symbol": "hk.12345"}).json()["status"] == "no_data"
    assert cl.get("/api/hk/stocks/lot", params={"symbol": "hk.00701"}).json()["lot"] == 400
    r = cl.post("/api/hk/stocks/job", json={"kind": "update"}).json()
    assert r["ok"] is True
    wait_job()
    assert cl.post("/api/hk/stocks/job", json={"kind": "bad"}).status_code == 400
    assert cl.post("/api/hk/stocks/stop").json()["job"]["running"] is False
    assert cl.get("/api/hk/stocks/status", params={"market": "HK"}).status_code == 200


def test_auto_refresh_also_updates_stocks_only_when_library_is_ready(loaded, monkeypatch):
    from server import valuation
    monkeypatch.setattr(hk, "_get", lambda params, timeout=25: {"klines": []})
    monkeypatch.setattr(valuation, "refresh", lambda conn, market="CN": {"ok": True, "errors": []})
    seen, scans = [], []
    monkeypatch.setattr(hk_stocks, "update_daily", lambda conn, **k: seen.append(1) or {"ok": 3, "empty": 0, "failed": []})
    from server import hk_scan
    monkeypatch.setattr(hk_scan, "run", lambda *a, **k: scans.append(1) or {"scan_date": "2026-11-06", "official": True, "candidates": []})
    with db.market_db("HK") as c:
        hk.refresh_all(c, "manual")
        assert seen == [] and scans == []                                                # 手动「立即更新」：只更新指数和估值
        info = hk.refresh_all(c, "auto")
        assert seen == [1] and info["stocks"] == {"ok": 3, "empty": 0} and scans == [1]  # 自动更新：个股日线一起增量，接着出今天的观察清单
        c.execute("DELETE FROM daily_bar")
        c.commit()
        hk.refresh_all(c, "auto")
        assert seen == [1] and scans == [1]                                               # 行情库还没初始化：不会自己去下 10 年日线，也不扫描
