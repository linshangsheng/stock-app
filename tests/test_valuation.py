"""宽基指数估值与分批建仓参考（server/valuation.py）：全部用合成数据 + 假的网络接口，不联网。"""
import json
import warnings
from datetime import date, datetime, timedelta, timezone

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

from server import db, valuation  # noqa: E402


def weekly(low=20.0, high=40.0, end="2026-11-05", years=10, low_at=2):
    """每周一个点，从 end 往前 years 年：中间一个最低点（离 end 约 low_at 年）、一个最高点。"""
    e = date.fromisoformat(end)
    n = int(years * 52)
    rows = []
    for k in range(n):
        d = e - timedelta(weeks=n - 1 - k)
        v = 30.0 + 2 * ((k % 7) - 3)                               # 24~36 之间来回
        rows.append([d.isoformat(), v])
    rows[n - int(low_at * 52)][1] = low
    rows[n // 3][1] = high
    rows[-1][1] = 30.0
    return rows


def cur_of(pe, date_="2026-11-06", pct=0.5):
    return {"pe": pe, "pb": 1.5, "pe_pct": pct, "pb_pct": 0.3, "yield": 0.02, "date": date_}


# ---------------------------------------------------------------- 规则
def test_gap_is_relative_to_the_ten_year_low_and_boundary_counts():
    rows = weekly(low=20.0)
    r = valuation.analyze_one("sh.000300", "沪深300", rows, cur_of(22.0))
    assert r["low"] == 20.0 and r["in_zone"] is True and abs(r["gap"] - 0.10) < 1e-9          # 最低 20：22 正好在建仓区的边上
    assert r["enter_price"] == 22.0 and r["batches_now"] == 1 and "第 1 批" in r["advice"]
    out = valuation.analyze_one("sh.000300", "沪深300", rows, cur_of(22.2))
    assert out["in_zone"] is False and out["batches_now"] == 0 and "还没到" in out["advice"] and "22.00" in out["advice"]


@pytest.mark.parametrize("pe,batches", [(23.0, 0), (22.0, 1), (21.5, 1), (21.3, 1), (21.2, 2), (21.0, 2), (20.7, 2), (20.6, 3), (20.5, 3), (20.2, 3), (20.1, 4), (20.0, 4)])
def test_tiers_accumulate_batches(pe, batches):
    r = valuation.analyze_one("sh.000300", "沪深300", weekly(low=20.0), cur_of(pe))
    assert r["batches_now"] == batches and r["batches_total"] == 5


def test_new_low_counts_as_the_bottom_and_keeps_last_batch_in_reserve():
    r = valuation.analyze_one("sz.399006", "创业板指", weekly(low=20.0), cur_of(18.5))
    assert r["low"] == 18.5 and r["gap"] == 0 and r["batches_now"] == 4 and "机动" in r["advice"]


def test_low_only_looks_at_the_last_ten_years():
    rows = weekly(low=20.0, years=12)
    rows[0][1] = 5.0                                                                          # 12 年前的极低值：不算
    r = valuation.analyze_one("sh.000300", "沪深300", rows, cur_of(22.0))
    assert r["low"] == 20.0


def test_not_enough_history_gives_nothing():
    assert valuation.analyze_one("sh.000300", "沪深300", [["2026-01-01", 10.0]] * 5, None) is None
    assert valuation.analyze_one("sh.000300", "沪深300", [], None) is None


def test_current_replaces_same_day_point_and_extends_history():
    rows = weekly(low=20.0)
    r = valuation.analyze_one("sh.000300", "沪深300", rows, cur_of(25.0, date_="2026-11-05"))      # 同一天：以最新接口值为准
    assert r["pe"] == 25.0 and r["asof"] == "2026-11-05" and r["points"] == len(rows)
    r2 = valuation.analyze_one("sh.000300", "沪深300", rows, cur_of(25.0, date_="2026-11-09"))
    assert r2["asof"] == "2026-11-09" and r2["points"] > r["points"] - 1


def test_percentile_is_share_of_history_at_or_below_current():
    rows = weekly(low=20.0)
    r = valuation.analyze_one("sh.000300", "沪深300", rows, cur_of(20.0))
    assert r["pct"] < 0.01
    r = valuation.analyze_one("sh.000300", "沪深300", rows, cur_of(39.0))
    assert r["pct"] > 0.99


def test_plan_follows_config(monkeypatch):
    cfg = {"tiers": [0.2, 0.1], "batches": 4, "min_days_between": 5}
    p = valuation.plan_for(0.15, cfg)
    assert p["batches_now"] == 1 and p["batches_total"] == 4 and p["per_batch_pct"] == 0.25 and p["min_days"] == 5


def test_timestamps_are_beijing_dates():
    assert valuation._day(1793894400000) == "2026-11-06"                                       # 北京时间 0 点


# ---------------------------------------------------------------- 取数与保存
def fake_http(rows_by_code, cur_by_code, fail_codes=(), fail_current=False):
    def http(url, timeout=25):
        if "pe_history/" in url:
            code = url.split("pe_history/")[1].split("?")[0]
            if code in fail_codes:
                raise RuntimeError("boom")
            return {"index_eva_pe_growths": [{"pe": v, "ts": int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp() * 1000) - 8 * 3600 * 1000} for d, v in rows_by_code[code]]}
        if fail_current:
            raise RuntimeError("down")
        return {"items": [{"index_code": c, "pe": x["pe"], "pb": 1.5, "pe_percentile": 0.1, "pb_percentile": 0.2, "yeild": 0.02,
                           "ts": int(datetime.fromisoformat(x["date"]).replace(tzinfo=timezone.utc).timestamp() * 1000) - 8 * 3600 * 1000}
                          for c, x in cur_by_code.items()]}
    return http


def all_rows(low=20.0):
    return {code: weekly(low=low) for code in valuation.CODES.values()}


def all_cur(pe=21.0):
    return {code: cur_of(pe) for code in valuation.CODES.values()}


def test_refresh_saves_and_merges_with_existing(demo_env, monkeypatch):
    monkeypatch.setattr(valuation, "_http", fake_http(all_rows(), all_cur(21.0)))
    with db.market_db("CN") as c:
        r = valuation.refresh(c)
        assert r["ok"] and not r["errors"]
        v = valuation.view_items(c)
        assert v["status"] == "ok" and set(v["items"]) == set(valuation.CODES) and v["items"]["sh.000300"]["batches_now"] == 2 and not v["stale"]
        # 接口只给最近一部分历史：库里已有的老历史不丢
        short = {code: rows[-20:] for code, rows in all_rows().items()}
        monkeypatch.setattr(valuation, "_http", fake_http(short, all_cur(24.0)))
        valuation.refresh(c)
        v2 = valuation.view_items(c)["items"]["sh.000300"]
        assert v2["low"] == 20.0 and v2["pe"] == 24.0 and v2["points"] > 400


def test_refresh_failure_keeps_old_data_and_never_raises(demo_env, monkeypatch):
    monkeypatch.setattr(valuation, "_http", fake_http(all_rows(), all_cur(21.0)))
    with db.market_db("CN") as c:
        valuation.refresh(c)
        before = db.get_meta(c, valuation.META)
        def down(url, timeout=25):
            raise RuntimeError("网络断了")
        monkeypatch.setattr(valuation, "_http", down)
        r = valuation.refresh(c)
        assert r["ok"] is False and r["errors"] and db.get_meta(c, valuation.META) == before          # 全部失败：原样保留
        monkeypatch.setattr(valuation, "_http", fake_http(all_rows(), all_cur(21.0), fail_codes=("SH000016",), fail_current=True))
        r = valuation.refresh(c)
        assert len(r["errors"]) == 2
        assert valuation.view_items(c)["items"]["sh.000016"]["pe"] == 21.0                           # 失败的那个指数用库里原来的


def test_first_use_starts_background_fetch_then_serves(demo_env, monkeypatch):
    monkeypatch.setattr(valuation, "is_demo", lambda: False)
    started = []
    monkeypatch.setattr(valuation, "refresh_async", lambda: started.append(1) or True)
    d = valuation.build()
    assert d["status"] == "loading" and started
    monkeypatch.setattr(valuation, "_http", fake_http(all_rows(), all_cur(21.0)))
    with db.market_db("CN") as c:
        valuation.refresh(c)
    d = valuation.build()
    assert d["status"] == "ok" and [i["name"] for i in d["items"]] == ["上证50", "沪深300", "中证500", "中证1000", "创业板指"]
    assert d["rule"]["enter_gap"] == 0.10 and d["rule"]["batches_total"] == 5 and "没有用历史回测验证" in d["note"]


def test_demo_mode_never_touches_the_network(demo_env, monkeypatch):
    called = []
    monkeypatch.setattr(valuation, "refresh_async", lambda: called.append(1))
    assert valuation.is_demo() and valuation.build()["status"] == "demo" and valuation.for_view() is None and not called


# ---------------------------------------------------------------- 接口
@pytest.fixture
def client(demo_env, monkeypatch):
    from server import main, market_view
    monkeypatch.setattr(valuation, "is_demo", lambda: False)
    monkeypatch.setattr(valuation, "refresh_async", lambda: True)
    monkeypatch.setattr(valuation, "_http", fake_http(all_rows(), all_cur(21.0)))
    with db.market_db("CN") as c:
        valuation.refresh(c)
        market_view.ensure_breadth(c, "CN")
    return TestClient(main.app)


def test_api_valuation_and_unsupported_market(client):
    d = client.get("/api/market/valuation").json()
    assert d["status"] == "ok" and len(d["items"]) == 5 and d["items"][1]["symbol"] == "sh.000300" and d["items"][1]["in_zone"] is True
    us = client.get("/api/market/valuation", params={"market": "US"}).json()
    assert us["status"] == "unsupported"


def test_market_view_carries_valuation_per_index(client):
    d = client.get("/api/market/view").json()
    assert d["status"] == "ok"
    by = {i["symbol"]: i for i in d["indices"]}
    assert by["sh.000300"]["valuation"]["batches_now"] == 2 and by["sz.399006"]["valuation"]["pe"] == 21.0
    assert d["valuation"]["rule"]["batches_total"] == 5 and d["valuation"]["note"]
    json.dumps(d)                                                                                # 可序列化
