"""内存优化的回归测试：面板分块加载（server/panel.py）、全市场上下文缓存永远只有一份（server/scanner.py）、归还内存（server/memtrim.py）。
面板的数值必须和原来经过 pd.read_sql_query 长表的路径逐位一致；峰值内存必须明显更低（防止有人又改回一次性读成长表）。"""
import gc
import tracemalloc

import numpy as np
import pandas as pd
import pytest

from server import db, memtrim, scanner, settings
from server import panel as P


def _same_panel(a, b):
    assert a.dates.equals(b.dates) and a.symbols.equals(b.symbols)
    assert a.dates.name == b.dates.name and a.symbols.name == b.symbols.name
    for name in ("volume", "amount", "turnover", "status", "is_st", "has_row", "is_temp"):
        x, y = getattr(a, name), getattr(b, name)
        assert x.equals(y) and x.dtypes.equals(y.dtypes), name
    for grp in ("raw", "adj"):
        for k, x in getattr(a, grp).items():
            y = getattr(b, grp)[k]
            assert x.equals(y) and x.dtypes.equals(y.dtypes), f"{grp}.{k}"


def _via_long_table(conn, start=None, end=None, symbols=None, only_l1=True, float32=True, market="CN"):
    """原来的做法：先读成一张长表，再铺成宽表。"""
    df = P.load_frame(conn, start, end, symbols, only_l1)
    splits = None
    if market == "US" and len(df):
        splits = pd.read_sql_query("SELECT symbol, ex_date, ratio_or_amount AS ratio FROM corp_actions WHERE type='split'", conn)
    return P.panel_from_frame(df, float32=float32, splits=splits)


def test_streamed_panel_equals_long_table_panel_cn(demo_env):
    with db.market_db("CN") as c:
        days = [r[0] for r in c.execute("SELECT DISTINCT date FROM daily_bar ORDER BY date")]
        syms = [r[0] for r in c.execute("SELECT symbol FROM securities ORDER BY symbol LIMIT 40")]
        for kw in (dict(), dict(float32=False), dict(start=days[-300], end=days[-10]), dict(symbols=syms[:7], only_l1=False),
                   dict(symbols=syms[:1], only_l1=False, float32=False), dict(symbols=["xx.000000"], only_l1=False)):
            _same_panel(P.load_panel(c, **kw), _via_long_table(c, **kw))


def test_streamed_panel_equals_long_table_panel_us_with_splits(us_template, tmp_path, monkeypatch):
    import shutil
    d = tmp_path / "usdata"
    shutil.copytree(us_template, d, ignore=shutil.ignore_patterns("*-wal", "*-shm", "portfolio.db*", "backups"))
    monkeypatch.setenv("STOCK_DATA_DIR", str(d))
    settings.load_config(reload=True)
    with db.market_db("US") as c:
        assert c.execute("SELECT COUNT(*) FROM corp_actions WHERE type='split'").fetchone()[0] > 0       # 确实带拆股，走了「真实价还原」那条分支
        _same_panel(P.load_panel(c, market="US"), _via_long_table(c, market="US"))
        _same_panel(P.load_panel(c, market="US", float32=False), _via_long_table(c, market="US", float32=False))
    settings.load_config(reload=True)


def test_null_values_become_nan_like_to_numeric(demo_env):
    """库里个别字段是 NULL（停牌 / 缺数据）：分块读取要和 pd.to_numeric(errors='coerce') 一样变成 NaN。"""
    with db.market_db("CN") as c:
        sym, day = c.execute("SELECT symbol, date FROM daily_bar ORDER BY date DESC LIMIT 1").fetchone()
        c.execute("UPDATE daily_bar SET turnover=NULL, amount=NULL WHERE symbol=? AND date=?", (sym, day))
        c.commit()
        new, old = P.load_panel(c, symbols=[sym], only_l1=False), _via_long_table(c, symbols=[sym], only_l1=False)
        _same_panel(new, old)
        assert np.isnan(new.turnover.loc[day, sym]) and np.isnan(new.amount.loc[day, sym])


def test_streaming_loader_has_a_much_lower_memory_peak_than_the_long_table(demo_env):
    """防止有人又改回「一次性读成一张长表」：这个数据量下，新做法的峰值应明显低于旧做法（实测真实数据 208 MB -> 77 MB，约 -63%）。"""
    with db.market_db("CN") as c:
        P.load_panel(c)                                                    # 预热（导入、缓存）
        _via_long_table(c)

        def peak(fn):
            gc.collect()
            tracemalloc.start()
            try:
                fn()
                return tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
        old, new = peak(lambda: _via_long_table(c)), peak(lambda: P.load_panel(c))
    assert new < 0.7 * old, f"分块加载的峰值 {new / 1e6:.1f} MB 没有比长表做法 {old / 1e6:.1f} MB 低多少"


def test_chunk_boundary_does_not_change_the_result(demo_env, monkeypatch):
    """分块大小只是内存 / 速度的取舍，不能影响结果：把块调得很小（每块 777 行）也要一样。"""
    with db.market_db("CN") as c:
        ref = P.load_panel(c)
        monkeypatch.setattr(P, "CHUNK", 777)
        _same_panel(P.load_panel(c), ref)


# ---- 全市场上下文缓存：永远只有一份；参数变了就失效 ----

@pytest.fixture
def counting_build(demo_env, monkeypatch):
    scanner._shared.clear()
    calls = []
    real = scanner.build_context

    def spy(conn, day, market="CN", lookback=scanner.LOOKBACK):
        assert not scanner._shared, "构建新上下文时，缓存里不应该还留着旧的那一份（内存里会同时有两份全市场数据）"
        calls.append(day)
        return real(conn, day, market, lookback)
    monkeypatch.setattr(scanner, "build_context", spy)
    yield calls
    scanner._shared.clear()


def test_shared_context_is_built_once(counting_build):
    a, day = scanner.shared_context("CN")
    b, day2 = scanner.shared_context("CN")
    assert a is b and day == day2 and len(counting_build) == 1


def test_changing_parameters_invalidates_the_cached_context(counting_build):
    scanner.shared_context("CN")
    cfg = settings.cfg()
    cfg["features"]["min_valid_ratio"] = cfg["features"]["min_valid_ratio"] / 2          # features 在参数指纹里
    scanner.shared_context("CN")
    assert len(counting_build) == 2                                              # 参数变了，重新构建


def test_run_scan_always_rebuilds_fresh_but_hands_the_result_to_the_cache(counting_build):
    scanner.shared_context("CN")                                                 # 缓存里先有一份
    r = scanner.run_scan("CN", persist=False)
    assert "error" not in r and len(counting_build) == 2                         # 扫描仍然用最新数据重新构建（行为不变）……
    scanner.shared_context("CN")
    assert len(counting_build) == 2                                              # ……并且把这份交给缓存：页面接口不用再建第三次


def test_scanning_a_past_day_does_not_publish_to_the_cache(counting_build):
    with db.market_db("CN") as c:
        days = [r[0] for r in c.execute("SELECT DISTINCT date FROM daily_bar ORDER BY date DESC LIMIT 5")]
    r = scanner.run_scan("CN", scan_date=days[3], persist=False)
    assert "error" not in r and not scanner._shared                              # 补跑的过去某天：用完即丢，不占缓存槽
    scanner.shared_context("CN")
    assert len(counting_build) == 2


def test_scan_result_does_not_depend_on_what_was_cached(demo_env):
    """缓存里有没有、缓存的是不是旧参数，都不能改变扫描结果。"""
    scanner._shared.clear()
    a = scanner.run_scan("CN", persist=False)
    scanner.shared_context("CN")
    b = scanner.run_scan("CN", persist=False)
    strip = lambda r: {k: v for k, v in r.items() if k not in ("run", "started_at", "finished_at")}   # noqa: E731
    assert [c["symbol"] for c in a["candidates"]] == [c["symbol"] for c in b["candidates"]]
    assert a["run"]["summary"] == b["run"]["summary"] if "run" in a else True


def test_memtrim_never_raises_and_has_an_off_switch(monkeypatch):
    monkeypatch.delenv("STOCKAPP_NO_TRIM", raising=False)             # 测试默认关掉它，这里要真的跑一次
    assert memtrim.trim() in (True, False)
    monkeypatch.setenv("STOCKAPP_NO_TRIM", "1")
    assert memtrim.trim() is False                                     # 开关：设为 1 就什么都不做


# ---- 缓存里丢掉的特征：必须真的没人用 ----

def test_dropped_features_are_not_referenced_anywhere_else():
    """CACHE_DROP_FEATURES 里的名字，除了 features.py 自己，不能在任何地方被引用（其他后端模块、配置、前端、测试、工具）。"""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    files = [f for f in [*root.glob("server/*.py"), *root.glob("server/*.yaml"), *root.glob("js/*.js"), *root.glob("tools/*.py"), *root.glob("tests/*.py")]
             if f.name not in ("features.py", "test_memory.py")]
    text = "\n".join(f.read_text(encoding="utf-8", errors="ignore") for f in files)
    text = re.sub(r"CACHE_DROP_FEATURES\s*=\s*\(.*?\)\n", "", text, flags=re.S)       # 清单自己写出的名字不算引用
    prefixes = set(re.findall(r"""f["']([a-z_]*)\{""", text))                       # 如 f"rps_{n}" 这类拼出来的名字
    for k in scanner.CACHE_DROP_FEATURES:
        assert not re.search(rf"""["']{k}["']""", text), f"{k} 在别处被用到了：请把它从 scanner.CACHE_DROP_FEATURES 里拿掉"
        assert not any(len(p) >= 2 and re.fullmatch(re.escape(p) + r"\d+", k) for p in prefixes), f"{k} 可能被 f-string（如 f\"{k[:-2]}{{n}}\"）拼出来用到：请确认后再丢"


def test_dropping_unused_features_does_not_change_scan_results(demo_env, monkeypatch):
    scanner._shared.clear()
    pruned = scanner.run_scan("CN", persist=False)
    ctx, _ = scanner.shared_context("CN")
    assert not any(k in ctx["feat"].f for k in scanner.CACHE_DROP_FEATURES)         # 确实丢掉了
    monkeypatch.setattr(scanner, "CACHE_DROP_FEATURES", ())
    full = scanner.run_scan("CN", persist=False)
    assert [(c["symbol"], c["setup"], round(c["score"], 6)) for c in pruned["candidates"]] == [(c["symbol"], c["setup"], round(c["score"], 6)) for c in full["candidates"]]
    assert pruned.get("summary") == full.get("summary") and pruned.get("regime") == full.get("regime")
