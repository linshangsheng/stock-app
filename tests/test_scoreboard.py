"""实盘成绩单：存档的信号 → 事后真实结果（对比基准）、按独立事件计数、结论分级、接口。"""
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

from server import db, market_calendar as mc, scoreboard as sb  # noqa: E402


def _days(c, n=60):
    return mc.trading_days(c)[-n:]


def _bench(c, d, k):
    return sb._fwd(sb._bars(c, "sh.000300"), d, k)


def test_fwd_matches_manual_open_to_close(demo_env):
    with db.market_db("CN") as c:
        days = _days(c, 40)
        d = days[3]
        rows = c.execute("SELECT date, open, close FROM index_bar WHERE symbol='sh.000300' AND date>? ORDER BY date LIMIT 5", (d,)).fetchall()
        assert abs(_bench(c, d, 5) - (rows[4][2] / rows[0][1] - 1)) < 1e-12              # 次日开盘买、第 5 个交易日收盘
        assert sb._fwd(sb._bars(c, "sh.000300"), days[-1], 5) is None                   # 还没到期


def test_panic_events_are_segments_not_days(demo_env):
    with db.market_db("CN") as c:
        days = _days(c, 80)
        seg1 = days[10:13]                                    # 连续 3 个恐慌日 = 同一段
        seg2 = days[30:31]                                    # 相隔 > 5 个交易日 = 新的一段
        for d in seg1 + seg2:
            for sym, r5, r20 in (("a", 0.04, 0.10), ("b", -0.02, 0.02)):
                c.execute("INSERT INTO panic_picks(date, symbol, name, ret_5d, ret_20d, filled) VALUES(?,?,?,?,?,1)", (d, sym, sym, r5, r20))
        out = sb._panic_stocks(c, "CN", sb._bars(c, "sh.000300"), mc.trading_days(c))
        assert out["events"] == 2 and out["rows"][0]["n"] == 2                           # 8 行记录、4 个恐慌日，只算 2 个独立事件
        r5 = out["rows"][0]
        assert abs(r5["avg"] - 0.01) < 1e-12                                              # 每个恐慌日 (4% − 2%)/2 = 1%
        exp = (sum(_bench(c, d, 5) for d in seg1) / 3 + _bench(c, seg2[0], 5)) / 2
        assert abs(r5["bench"] - exp) < 1e-12 and abs(r5["excess"] - (0.01 - exp)) < 1e-12
        assert out["verdict"]["level"] == "none" and "样本太少" in out["verdict"]["text"]  # 2 个事件不下结论


def test_watchlist_counts_empty_days_and_dedups_reruns(demo_env):
    with db.market_db("CN") as c:
        days = _days(c, 60)
        mk = lambda rid, d, n: c.execute("INSERT INTO scan_runs(run_id, market, scan_date, official, n_candidates, finished_at) VALUES(?,?,?,1,?,?)", (rid, "CN", d, n, d + "T20"))  # noqa: E731
        mk("r0", days[5], 0)                                                                  # 空仓日：不算事件
        mk("r1", days[10], 2); mk("r1b", days[10], 2)                                          # 同一天重跑，只取后一次
        for rid, sym, r5 in (("r1", "x", 0.50), ("r1b", "x", 0.06), ("r1b", "y", 0.02)):
            c.execute("INSERT INTO scan_outcomes(run_id, symbol, ret_5d, ret_20d, filled) VALUES(?,?,?,NULL,1)", (rid, sym, r5))
        out = sb._watchlist(c, "CN", sb._bars(c, "sh.000300"))
        assert out["runs"] == 2 and out["empty_runs"] == 1 and out["events"] == 1
        assert abs(out["rows"][0]["avg"] - 0.04) < 1e-12 and out["rows"][1]["n"] == 0 and out["rows"][1]["pending"] == 1


def test_etf_rule_live_uses_open_close_convention(demo_env):
    with db.market_db("CN") as c:
        days = _days(c, 30)
        for d in days[5:12]:                                   # 满仓
            c.execute("INSERT INTO signal_log(date, kind, key, value) VALUES(?,?,?,1.0)", (d, "etf_rule", "sh.000300"))
        for d in days[12:16]:                                  # 空仓
            c.execute("INSERT INTO signal_log(date, kind, key, value) VALUES(?,?,?,0.0)", (d, "etf_rule", "sh.000300"))
        row = next(r for r in sb._rule_live(c, "CN")["rows"] if r["symbol"] == "sh.000300")
        bars = sb._bars(c, "sh.000300").set_index("date")

        def day_ret(t, p_prev, p_exec):
            i = list(bars.index).index(t)
            o, cl, pc = bars["open"].iloc[i], bars["close"].iloc[i], bars["close"].iloc[i - 1]
            return p_prev * (o / pc - 1) + p_exec * (cl / o - 1)
        # 只有「前一天、前两天都有存档」的交易日才计入：days[7] .. days[16]
        used = days[7:17]
        pos = {d: (1.0 if d in days[5:12] else 0.0) for d in days[5:17]}
        idx = list(bars.index)
        prod, hold = 1.0, 1.0
        from server import market_view
        cost = market_view.mv_cfg()["index_rule"]["cost_per_side"]
        for t in used:
            t1, t2 = idx[idx.index(t) - 1], idx[idx.index(t) - 2]
            prod *= 1 + day_ret(t, pos[t2], pos[t1]) - abs(pos[t1] - pos[t2]) * cost
            hold *= 1 + bars["close"].loc[t] / bars["close"].iloc[idx.index(t) - 1] - 1
        assert row["days"] == len(used) and abs(row["rule"] - (prod - 1)) < 1e-9 and abs(row["hold"] - (hold - 1)) < 1e-9
        assert row["pos"] == 0.0 and row["since"] == days[5]


def test_verdict_levels():
    assert sb._verdict(0, "x")["level"] == "none" and sb._verdict(9, "x")["level"] == "none"
    assert sb._verdict(10, "x")["level"] == "few" and sb._verdict(29, "x")["level"] == "few" and sb._verdict(30, "x")["level"] == "ok"


def test_record_is_idempotent_and_never_rewrites(demo_env):
    with db.market_db("CN") as c:
        d = _days(c, 5)[-1]
        c.execute("INSERT INTO signal_log(date, kind, key, value) VALUES(?,?,?,0.5)", (d, "etf_rule", "sh.000300"))
        n = c.execute("INSERT OR IGNORE INTO signal_log(date, kind, key, value) VALUES(?,?,?,1.0)", (d, "etf_rule", "sh.000300")).rowcount
        assert n == 0 and c.execute("SELECT value FROM signal_log WHERE date=?", (d,)).fetchone()[0] == 0.5


def test_scoreboard_api(demo_env):
    from server import main
    c = TestClient(main.app)
    j = c.get("/api/scoreboard", params={"market": "CN"}).json()
    assert j["status"] == "ok" and j["market"] == "CN" and j["bench"] == "沪深300"
    ids = [s["id"] for s in j["signals"]]
    assert ids[:2] == ["watchlist", "panic_stocks"] and any(i.startswith("panic_etf_") for i in ids)
    assert j["rule"]["id"] == "etf_rule" and "不是回测" in j["note"]
    u = c.get("/api/scoreboard", params={"market": "US"}).json()
    assert u["market"] == "US" and u["bench"].startswith("标普500")


def _syms(c, n=4):
    return [r[0] for r in c.execute("SELECT symbol FROM securities WHERE in_l1=1 ORDER BY symbol LIMIT ?", (n,)).fetchall()]


def test_factor_live_settles_a_period_open_to_open(demo_env):
    from server import factor_portfolio as fp
    every = int(fp.fp_cfg()["rebalance_days"])
    with db.market_db("CN") as c:
        t = mc.trading_days(c)
        d0, d1 = t[-every - 5], t[-5]                                     # 相隔正好一个调仓周期的两次存档
        names = _syms(c)
        for d in (d0, d1):
            for k, s in enumerate(names):
                c.execute("INSERT INTO signal_log(date, kind, key, value, detail) VALUES(?,?,?,?,?)", (d, "factor_target", s, k + 1, '{"name":"' + s + '"}'))
        out = sb._factor_live(c, "CN")
        entry, exit_ = t[t.index(d0) + 1], t[t.index(d1) + 1]
        rs = []
        for s in names:
            a = c.execute("SELECT open*adj_factor FROM daily_bar WHERE symbol=? AND date=?", (s, entry)).fetchone()
            b = c.execute("SELECT open*adj_factor FROM daily_bar WHERE symbol=? AND date=?", (s, exit_)).fetchone()
            if a and b and a[0] and b[0]:
                rs.append(b[0] / a[0] - 1)
        bo = {d: o for d, o in c.execute("SELECT date, open FROM index_bar WHERE symbol='sh.000300' AND date IN (?,?)", (entry, exit_))}
        assert out["events"] == 1 and out["horizons"] == [20]
        r = out["rows"][0]
        assert abs(r["avg"] - sum(rs) / len(rs)) < 1e-12 and abs(r["bench"] - (bo[exit_] / bo[entry] - 1)) < 1e-12
        assert out["current"]["date"] == d1 and out["current"]["n"] == len(names)         # 最新一期还没结束：只给估算，不计入成绩
        assert out["verdict"]["level"] == "none"


def test_factor_live_skips_broken_chain(demo_env):
    from server import factor_portfolio as fp
    every = int(fp.fp_cfg()["rebalance_days"])
    with db.market_db("CN") as c:
        t = mc.trading_days(c)
        d0, d1 = t[-2 * every - 3], t[-every - 1]                            # 间隔不是一个整周期（漏了一期）：不结算，不拿跨期的收益冒充一期
        assert t.index(d1) - t.index(d0) != every
        for d in (d0, d1):
            c.execute("INSERT INTO signal_log(date, kind, key, value, detail) VALUES(?,?,?,?,?)", (d, "factor_target", _syms(c, 1)[0], 1, "{}"))
        assert sb._factor_live(c, "CN")["events"] == 0


def test_record_factor_only_on_rebalance_day_and_never_rewrites(demo_env):
    from server import factor_portfolio as fp, settings
    every = int(fp.fp_cfg()["rebalance_days"])
    with db.market_db("CN") as c:
        t = mc.trading_days(c)
        asof = db.get_meta(c, "data_asof")
    pos = t.index(asof)
    settings.cfg()["factor_portfolio"]["anchor"] = t[pos - 1]                       # 数据截止日是调仓日的后一天：不是调仓日，什么都不存
    assert sb.record_factor("CN") == 0
    settings.cfg()["factor_portfolio"]["anchor"] = t[pos - 2 * every]               # 数据截止日恰好是调仓日
    n = sb.record_factor("CN")
    assert 0 < n <= int(fp.fp_cfg()["n"])
    with db.market_db("CN") as c:
        rows = c.execute("SELECT key, value FROM signal_log WHERE kind='factor_target' AND date=?", (asof,)).fetchall()
        assert len(rows) == n and sorted(r[1] for r in rows) == [r[1] for r in sorted(rows, key=lambda r: r[1])]
    assert sb.record_factor("CN") == 0                                              # 同一天只存第一次
    assert sb.record_factor("US") == 0                                              # 美股没有低风险组合
