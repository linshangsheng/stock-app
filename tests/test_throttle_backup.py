"""限速 / 退避 / 熔断（3.26.3）与备份恢复（3.26.6）。"""
import random
from datetime import date, timedelta

import pytest

from server import backup, db, portfolio, settings
from server.throttle import CircuitOpen, Throttle


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make(clock, **kw):
    return Throttle("t", min_delay=1, max_delay=2, backoff_base=10, backoff_max=80, breaker_failures=3, breaker_pause=600,
                    sleep=clock.sleep, clock=clock, rng=random.Random(1), **kw)


def test_exponential_backoff_then_success():
    clk = Clock()
    th = make(clk)
    calls = []

    def f():
        calls.append(clk.t)
        if len(calls) < 3:
            raise RuntimeError("429")
        return "ok"

    assert th.call(f, retries=3) == "ok"
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    assert gaps[0] >= 10 and gaps[1] >= 20, f"指数退避 10s → 20s，实际间隔 {gaps}"


def test_circuit_breaker_opens_and_recovers():
    clk = Clock()
    th = make(clk)

    def boom():
        raise RuntimeError("blocked")

    with pytest.raises(CircuitOpen):
        th.call(boom, retries=5)
    assert th.is_open
    with pytest.raises(CircuitOpen):
        th.call(lambda: "x")
    clk.t += 601
    assert not th.is_open and th.call(lambda: "x") == "x"


def test_random_delay_between_requests():
    clk = Clock()
    th = make(clk)
    for _ in range(5):
        th.call(lambda: 1)
    assert clk.t >= 4 * 1.0, "请求之间必须有 ≥ min_delay 的随机延迟"


def test_daily_budget_for_scarce_resources():
    clk = Clock()
    th = make(clk, daily_budget=2)
    th.call(lambda: 1)
    th.call(lambda: 1)
    with pytest.raises(CircuitOpen):
        th.call(lambda: 1)


def _seed_portfolio():
    portfolio.set_account("CN", 1_000_000, 1_000_000, 0.005)
    with db.portfolio_db() as p:
        p.execute("INSERT INTO watchlist(market,symbol,added_at) VALUES('CN','sh.600001','2026-01-01')")
        p.execute("INSERT INTO positions(market,symbol,open_date,qty,avg_cost,initial_stop,current_stop,status,init_qty) "
                  "VALUES('CN','sh.600001','2026-01-02',500,10,9,9,'open',500)")


def test_backup_and_restore_roundtrip(demo_env, monkeypatch):
    monkeypatch.setitem(settings.cfg()["backup"], "dir", str(demo_env / "bk"))
    _seed_portfolio()
    info = backup.run_backup(day="2026-10-01", full=False)
    assert info["files"]["portfolio.db"]["positions"] == 1
    assert "scan_runs" in info["files"]["essential_ashare.db"]
    with db.portfolio_db() as p:
        p.execute("DELETE FROM positions")
        p.execute("DELETE FROM watchlist")
    out = backup.restore("2026-10-01", "portfolio")
    assert out["portfolio.db"]["positions"] == 1 and out["portfolio.db"]["watchlist"] == 1
    with db.portfolio_db() as p:
        assert p.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1


def test_prune_keeps_14_daily_and_monthly(fresh_env, monkeypatch):
    monkeypatch.setitem(settings.cfg()["backup"], "dir", str(fresh_env / "bk"))
    root = backup.backup_dir()
    d0 = date(2026, 10, 3)
    for i in range(0, 200):
        (root / (d0 - timedelta(days=i)).isoformat()).mkdir()
    backup.prune()
    left = sorted(p.name for p in root.iterdir())
    assert len([n for n in left if n >= (d0 - timedelta(days=13)).isoformat()]) == 14        # 近 14 天日备
    months = {n[:7] for n in left}
    assert len(months) <= 14 and "2026-05" in months                                          # 月备保留
    assert len(left) < 40
