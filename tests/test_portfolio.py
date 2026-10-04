"""持仓 / 交易日志 / 体检 / 复盘统计（5.10 / 5.11）。"""
import pytest

from server import db, portfolio


def test_trade_validation_rules(demo_env):
    with pytest.raises(ValueError, match="止损"):
        portfolio.record_trade("CN", {"symbol": "sh.600001", "side": "buy", "date": "2026-09-01", "price": 10, "qty": 100})
    portfolio.record_trade("CN", {"symbol": "sh.600001", "side": "buy", "date": "2026-09-01", "price": 10, "qty": 1000, "initial_stop": 9})
    with pytest.raises(ValueError, match="出场原因"):
        portfolio.record_trade("CN", {"symbol": "sh.600001", "side": "sell", "date": "2026-09-10", "price": 11, "qty": 500})
    with pytest.raises(ValueError, match="备注"):
        portfolio.record_trade("CN", {"symbol": "sh.600001", "side": "sell", "date": "2026-09-10", "price": 11, "qty": 500, "exit_reason": "主观"})
    with pytest.raises(ValueError, match="超过持仓"):
        portfolio.record_trade("CN", {"symbol": "sh.600001", "side": "sell", "date": "2026-09-10", "price": 11, "qty": 5000, "exit_reason": "止损"})


def test_round_trip_pnl_and_r_multiple(demo_env):
    portfolio.record_trade("CN", {"symbol": "sh.600001", "side": "buy", "date": "2026-09-01", "price": 10, "qty": 1000,
                                  "initial_stop": 9, "setup": "breakout", "fee": 0})
    portfolio.record_trade("CN", {"symbol": "sh.600001", "side": "sell", "date": "2026-09-10", "price": 12, "qty": 1000,
                                  "exit_reason": "移动止盈", "fee": 0})
    assert portfolio.list_positions("CN", "open") == []
    st = portfolio.journal_stats("CN")
    assert st["overall"]["n"] == 1
    assert st["overall"]["expectancy_r"] == pytest.approx(2.0)            # 盈利 2000 / 计划风险 1000
    assert st["overall"]["small_sample"] is True                          # 样本不足须标注，不下结论


def test_partial_sell_keeps_position(demo_env):
    portfolio.record_trade("CN", {"symbol": "sh.600001", "side": "buy", "date": "2026-09-01", "price": 10, "qty": 1000, "initial_stop": 9})
    portfolio.record_trade("CN", {"symbol": "sh.600001", "side": "sell", "date": "2026-09-05", "price": 11, "qty": 400, "exit_reason": "移动止盈"})
    pos = portfolio.list_positions("CN", "open")
    assert len(pos) == 1 and pos[0]["qty"] == 600


def test_health_flags_stop_hit_and_raises_stop(demo_env):
    with db.market_db("CN") as c:
        asof = db.get_meta(c, "data_asof")
        row = c.execute("SELECT symbol, close FROM daily_bar WHERE date=? AND trade_status=1 ORDER BY symbol LIMIT 1", (asof,)).fetchone()
        sym, px = row["symbol"], row["close"]
        d0 = c.execute("SELECT date FROM daily_bar WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 8", (sym, asof)).fetchall()[-1][0]
    # 持仓成本远低于现价 -> 浮盈；止损设在现价之上 -> 触发
    portfolio.record_trade("CN", {"symbol": sym, "side": "buy", "date": d0, "price": px * 0.8, "qty": 1000, "initial_stop": px * 0.7})
    h = portfolio.health("CN", asof, save=False)
    p = h["positions"][0]
    assert p["r_multiple"] > 0 and p["current_stop"] >= px * 0.7
    with db.portfolio_db() as pdb:
        pdb.execute("UPDATE positions SET current_stop=?", (px * 1.5,))
    h2 = portfolio.health("CN", asof, save=False)
    assert h2["positions"][0]["level"] == "must" and "止损" in "".join(h2["positions"][0]["reasons"])
    assert h2["summary"]["levels"]["must"] == 1
