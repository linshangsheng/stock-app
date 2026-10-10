"""检验通过标准（config.validation）与备份目录拆分。"""
from server import backtest, backup, settings


def h(ic, ir=0.5, t=3.0, mono=1.0, ls=0.01):
    return {"ic_mean": ic, "ic_ir": ir, "t_stat_eff": t, "monotonic": mono, "long_short": ls}


def test_factor_verdict_requires_multiple_horizons_and_consistent_direction():
    good = {"horizons": {5: h(0.05), 10: h(0.06), 20: h(0.07)}}
    assert backtest.factor_verdict(good, (5, 10, 20))["pass"] and backtest.factor_verdict(good, (5, 10, 20))["direction"] == "正向"
    rev = {"horizons": {5: h(-0.05, ls=-0.01), 10: h(-0.06, ls=-0.01), 20: h(0.0)}}
    v = backtest.factor_verdict(rev, (5, 10, 20))
    assert v["pass"] and v["direction"] == "反向"                              # 方向不预设：反转因子同样可通过
    weak = {"horizons": {5: h(0.05), 10: h(0.005), 20: h(0.004)}}
    assert not backtest.factor_verdict(weak, (5, 10, 20))["pass"]
    noisy = {"horizons": {5: h(0.05, t=1.0), 10: h(0.06, t=1.2), 20: h(0.07)}}
    assert not backtest.factor_verdict(noisy, (5, 10, 20))["pass"]
    nonmono = {"horizons": {5: h(0.05, mono=0.2), 10: h(0.06, mono=0.1), 20: h(0.07, mono=0.3)}}
    assert not backtest.factor_verdict(nonmono, (5, 10, 20))["pass"]


def ev(**kw):
    base = {"independent_signal_days": 200, "mean_r_by_day": 0.4, "random_baseline": {"p_value": 0.01}, "mean_r_ci95": [0.1, 0.7],
            "by_year": {"2022": {"n": 50, "mean_r": 0.5}, "2023": {"n": 50, "mean_r": 0.3}, "2024": {"n": 50, "mean_r": -0.1}}}
    base.update(kw)
    return base


def test_event_verdict_rules():
    assert backtest.event_verdict(ev())[1] is True
    assert "不下结论" in backtest.event_verdict(ev(independent_signal_days=30))[0]
    assert backtest.event_verdict(ev(random_baseline={"p_value": 0.2}))[1] is False
    assert backtest.event_verdict(ev(mean_r_ci95=[-0.1, 0.9]))[1] is False
    assert backtest.event_verdict(ev(by_year={"2022": {"n": 50, "mean_r": 0.5}, "2023": {"n": 50, "mean_r": -0.3}, "2024": {"n": 50, "mean_r": -0.1}}))[1] is False


def test_full_backup_goes_to_separate_dir(demo_env, monkeypatch):
    monkeypatch.setitem(settings.cfg()["backup"], "dir", str(demo_env / "daily"))
    monkeypatch.setitem(settings.cfg()["backup"], "full_dir", str(demo_env / "full"))
    info = backup.run_backup(day="2026-10-02", full=True)
    assert (demo_env / "full" / "2026-10-02" / "ashare.db.gz").exists(), "全量备份压缩存放"
    assert (demo_env / "daily" / "2026-10-02" / "essential_ashare.db.gz").exists()
    assert not (demo_env / "daily" / "2026-10-02" / "ashare.db").exists(), "大体积全量备份不进（可能是网盘的）每日目录"
