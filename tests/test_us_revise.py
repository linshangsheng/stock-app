"""美股：收盘后不久取到的 Yahoo 初步 K 线（开盘价落在最高 / 最低价之外）会让闸门 price_sanity 不过；
闸门标出的异常股最近几天重拉一遍即可修复；增量更新时重叠日也用上游修正后的数据覆盖（离线，演示数据）。"""
from server import db, ingest, quality
from server.datasource_cn import get_source


def _corrupt(c, day, n):
    """把 n 只股票当日的开盘价改到最高价之上（模拟 Yahoo 的初步数据）。"""
    syms = [r[0] for r in c.execute("SELECT symbol FROM daily_bar WHERE date=? AND trade_status>0 ORDER BY symbol LIMIT ?", (day, n))]
    c.executemany("UPDATE daily_bar SET open=high*1.003 WHERE symbol=? AND date=?", [(s, day) for s in syms])
    c.commit()
    return syms


def test_gate_flags_preliminary_bars_and_refetch_repairs_them(us_env):
    with db.market_db("US") as c:
        day = c.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
        live = c.execute("SELECT COUNT(*) FROM daily_bar WHERE date=? AND trade_status>0", (day,)).fetchone()[0]
        syms = _corrupt(c, day, max(5, int(live * 0.05)))
        g = quality.check_gate(c, "US", day)
        ps = next(x for x in g["checks"] if x["name"] == "price_sanity")
        assert not ps["ok"] and set(syms) <= set(g["bad_symbols"])
        assert "超出当日最高最低价" in ps["msg"] and "自动重新拉取" in ps["msg"]
        r = ingest.refetch_recent(c, get_source("US"), "US", g["bad_symbols"], day)
        assert r["refetched"] >= len(syms)
        g2 = quality.check_gate(c, "US", day)
        assert next(x for x in g2["checks"] if x["name"] == "price_sanity")["ok"]
        o, h = c.execute("SELECT open, high FROM daily_bar WHERE symbol=? AND date=?", (syms[0], day)).fetchone()
        assert o <= h + 1e-9


def test_incremental_overwrites_overlap_day_with_revised_data(us_env):
    with db.market_db("US") as c:
        days = [r[0] for r in c.execute("SELECT DISTINCT date FROM daily_bar ORDER BY date DESC LIMIT 2")]
        last, prev = days[0], days[1]
        sym = c.execute("SELECT symbol FROM daily_bar WHERE date=? AND trade_status>0 ORDER BY symbol LIMIT 1", (last,)).fetchone()[0]
        c.execute("DELETE FROM daily_bar WHERE symbol=? AND date=?", (sym, last))       # 库里最后一天变成 prev
        c.execute("UPDATE daily_bar SET open=high*1.003 WHERE symbol=? AND date=?", (sym, prev))   # prev 是初步数据
        c.commit()
        row = c.execute("SELECT date, close, adj_factor FROM daily_bar WHERE symbol=? AND date=?", (sym, prev)).fetchone()
        r = ingest.update_incremental_batch(c, get_source("US"), "US", [sym], {sym: tuple(row)}, last)
        assert r["updated"] == 1
        o, h = c.execute("SELECT open, high FROM daily_bar WHERE symbol=? AND date=?", (sym, prev)).fetchone()
        assert o <= h + 1e-9                                                             # 重叠日已被覆盖成修正后的数据
        assert c.execute("SELECT 1 FROM daily_bar WHERE symbol=? AND date=?", (sym, last)).fetchone()
