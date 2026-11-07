"""10 年期国债收益率与股债利差（server/bonds.py + valuation 里的股债利差）：假的东财数据中心，不联网。"""
import warnings
from datetime import date, timedelta

import pandas as pd
import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

from server import bonds, db, valuation  # noqa: E402
from tests.test_valuation import all_cur, all_rows, cur_of, fake_http, hk_rows, weekly  # noqa: E402

CN, US = "EMM00166466", "EMG00001310"


def fake_pages(start="2015-10-01", end="2026-11-06", cn0=3.0, step=0.0004, us_const=4.0, calls=None, fail=False):
    """东财数据中心的假数据：每个工作日一行，按日期从新到旧，每页 500 行。"""
    days = list(pd.bdate_range(start, end))
    rows = [{"SOLAR_DATE": f"{d.date()} 00:00:00", CN: round(cn0 - step * i, 4), US: us_const} for i, d in enumerate(days)][::-1]

    def page(n, size=500, timeout=90):
        if calls is not None:
            calls.append(n)
        if fail:
            raise RuntimeError("断网")
        return {"pages": (len(rows) + size - 1) // size, "count": len(rows), "data": rows[(n - 1) * size:n * size]}
    return page


# ---------------------------------------------------------------- 解析与取数
def test_rows_parse_ignores_empty_and_absurd_values():
    out = bonds._rows({"data": [{"SOLAR_DATE": "2026-11-06 00:00:00", CN: 1.5868, US: 4.86}, {"SOLAR_DATE": "2026-11-05 00:00:00", CN: None, US: 4.84},
                                {"SOLAR_DATE": "2026-11-04 00:00:00", CN: 0, US: 999}]})
    assert out == {"CN": [["2026-11-06", 1.5868]], "US": [["2026-11-06", 4.86], ["2026-11-05", 4.84]]}


def test_full_fetch_merges_all_pages_sorted_and_incremental_fetches_only_the_first(monkeypatch):
    calls = []
    monkeypatch.setattr(bonds, "_page", fake_pages(calls=calls))
    full = bonds.fetch(True)
    assert sorted(calls) == [1, 2, 3, 4, 5, 6] and len(full["CN"]) == len(pd.bdate_range("2015-10-01", "2026-11-06")) and len(full["US"]) == len(full["CN"])
    assert full["CN"][0][0] < full["CN"][-1][0] and full["CN"][-1][0] == "2026-11-06"                 # 日期升序
    calls.clear()
    inc = bonds.fetch(False)
    assert calls == [1] and len(inc["CN"]) == 500                                                      # 增量：只取最新一页


def test_refresh_stores_per_market_with_hk_using_the_us_series_and_fetches_once(demo_env, monkeypatch):
    calls = []
    monkeypatch.setattr(bonds, "_page", fake_pages(calls=calls))
    bonds._mem.update({"at": 0.0, "rows": None})
    with db.market_db("CN") as c_cn, db.market_db("US") as c_us, db.market_db("HK") as c_hk:
        assert bonds.refresh(c_cn, "CN")["ok"] and bonds.refresh(c_us, "US")["ok"] and bonds.refresh(c_hk, "HK")["ok"]
        n_calls = len(calls)
        assert n_calls == 6                                                                           # 三个市场共用一次联网（6 页），不是 18 页
        cn, us, hk = bonds.load(c_cn, "CN"), bonds.load(c_us, "US"), bonds.load(c_hk, "HK")
    assert cn["series"] == "CN" and us["series"] == "US" and hk["series"] == "US" and hk["rows"] == us["rows"] and cn["rows"][-1][1] != us["rows"][-1][1]
    assert us["rows"][-1][1] == 4.0 and bonds.stale(cn) is False


def test_refresh_failure_keeps_old_rows_and_does_not_raise(demo_env, monkeypatch):
    bonds._mem.update({"at": 0.0, "rows": None})
    monkeypatch.setattr(bonds, "_page", fake_pages())
    with db.market_db("CN") as c:
        assert bonds.refresh(c, "CN")["ok"]
        before = db.get_meta(c, "bond_yield_cn")
        bonds._mem.update({"at": 0.0, "rows": None})
        monkeypatch.setattr(bonds, "_page", fake_pages(fail=True))
        r = bonds.refresh(c, "CN")
        assert r["ok"] is False and "断网" in r["error"] and db.get_meta(c, "bond_yield_cn") == before


# ---------------------------------------------------------------- 查日期、摘要
def test_yield_on_uses_previous_trading_day_but_not_too_old():
    rows = [["2026-11-05", 1.5], ["2026-11-06", 1.6]]                                                  # 周四、周五
    assert bonds.yield_on(rows, "2026-11-06") == 1.6 and bonds.yield_on(rows, "2026-11-08") == 1.6          # 周日 → 周五的值
    assert bonds.yield_on(rows, "2026-11-04") is None                                                  # 比最早的还早
    assert bonds.yield_on(rows, "2026-12-30") is None                                                  # 隔了一个多月：不拿旧值冒充


def test_info_has_one_year_change_and_hk_note():
    rows = [[(date(2024, 1, 1) + timedelta(days=i)).isoformat(), 2.0 + i * 0.001] for i in range(300)]
    cn, hk = bonds.info(rows, "CN"), bonds.info(rows, "HK")
    assert cn["name"] == "中国10年期国债" and cn["yield"] == rows[-1][1] and cn["chg_1y"] == round(rows[-1][1] - rows[-251][1], 2) and "note" not in cn
    assert hk["name"] == "美国10年期国债" and "联系汇率" in hk["note"] and bonds.info([], "CN") is None


# ---------------------------------------------------------------- 股债利差
def flat_bond(y, start="2015-10-01", end="2026-11-06"):
    return [[d.date().isoformat(), y] for d in pd.bdate_range(start, end)]


def test_spread_is_earnings_yield_minus_bond_yield_with_percentile_and_label():
    item = {"pe": 12.5, "asof": "2026-11-06"}
    pts = [(d, pe) for d, pe in [(r[0], 20.0 if i < 400 else 12.5) for i, r in enumerate(weekly(low=11.0))]]            # 大部分时间市盈率 12.5（盈利收益率 8%）
    bonds.add_spread(item, pts, flat_bond(2.0))
    assert item["ey"] == 8.0 and item["bond_yield"] == 2.0 and item["spread"] == 6.0
    assert item["spread_label"] == "股票相对便宜" and item["spread_pct"] >= 0.8 and item["spread_max"] == 6.0 and item["spread_min"] == 3.0 and item["spread_points"] > 400
    pricey = {"pe": 40.0, "asof": "2026-11-06"}
    bonds.add_spread(pricey, [(d, 12.5) for d, _ in pts], flat_bond(2.0))                                           # 历史上都比现在便宜
    assert pricey["spread"] == pytest.approx(0.5, abs=0.01) and pricey["spread_label"] == "股票相对偏贵" and pricey["spread_pct"] <= 0.2
    mid = {"pe": 16.0, "asof": "2026-11-06"}
    bonds.add_spread(mid, [(d, 12.5 if i % 2 else 20.0) for i, (d, _) in enumerate(pts)], flat_bond(2.0))
    assert mid["spread_label"] == "中等"


def test_spread_needs_data_and_does_not_invent_percentile_from_a_short_history():
    item = {"pe": 12.5, "asof": "2026-11-06"}
    bonds.add_spread(item, [("2026-10-30", 12.0)], [])
    assert "spread" not in item                                                                         # 没有国债数据：不给
    bonds.add_spread(item, [("2026-10-30", 12.0)], flat_bond(2.0))
    assert item["spread"] == 6.0 and "spread_pct" not in item                                           # 历史不足一年：有利差但不给分位
    old = {"pe": 12.5, "asof": "2026-11-06"}
    bonds.add_spread(old, [("2026-10-30", 12.0)], flat_bond(2.0, end="2026-06-01"))
    assert "spread" not in old                                                                          # 国债数据太旧（比估值日早很多）：不拿来凑


def test_analyze_one_adds_spread_only_when_bond_given():
    rows = weekly(low=20.0)
    plain = valuation.analyze_one("sh.000300", "沪深300", rows, cur_of(21.0))
    withb = valuation.analyze_one("sh.000300", "沪深300", rows, cur_of(21.0), bond=flat_bond(1.5))
    assert "spread" not in plain and withb["spread"] == pytest.approx(100 / 21 - 1.5, abs=0.01) and "spread_label" in withb


# ---------------------------------------------------------------- 接口
@pytest.fixture
def client(demo_env, monkeypatch):
    from server import main, market_view
    monkeypatch.setattr(valuation, "is_demo", lambda m="CN": False)
    monkeypatch.setattr(valuation, "refresh_async", lambda m="CN": True)
    monkeypatch.setattr(bonds, "refresh_async", lambda m: True)
    monkeypatch.setattr(valuation, "_http", fake_http({**all_rows(), "SP500": weekly(low=16.0), "NDX": weekly(low=19.0)},
                                                      {**all_cur(21.0), "SP500": cur_of(23.0), "NDX": cur_of(26.0)}))
    monkeypatch.setattr(bonds, "_page", fake_pages())
    bonds._mem.update({"at": 0.0, "rows": None})
    from server import settings
    for m in ("CN", "US", "HK"):
        with settings.market_ctx(m), db.market_db(m) as c:
            valuation.refresh(c, m)
            bonds.refresh(c, m)
            if m != "HK":
                market_view.ensure_breadth(c, m)
    return TestClient(main.app)


def test_market_view_carries_spread_and_bond_for_cn(client):
    d = client.get("/api/market/view", params={"market": "CN"}).json()
    v = {i["symbol"]: i.get("valuation") for i in d["indices"]}["sh.000300"]
    assert v["spread"] is not None and v["spread_label"] in ("股票相对便宜", "中等", "股票相对偏贵") and v["bond_yield"] > 0
    assert d["valuation"]["bond"]["name"] == "中国10年期国债" and d["valuation"]["bond"]["yield"] > 0


def test_us_valuation_api_uses_us_bond_without_the_hk_note(client):
    d = client.get("/api/market/valuation", params={"market": "US"}).json()
    assert d["status"] == "ok" and d["bond"]["name"] == "美国10年期国债" and "note" not in d["bond"]
    assert [i["symbol"] for i in d["items"]] == ["SPY", "QQQ"] and all(i["spread"] is not None for i in d["items"])


def test_hk_valuation_api_uses_us_bond_with_note(client):
    d = client.get("/api/market/valuation", params={"market": "HK"}).json()
    assert d["status"] == "ok" and d["bond"]["name"] == "美国10年期国债" and "联系汇率" in d["bond"]["note"]
    assert all(i["spread"] is not None for i in d["items"])
