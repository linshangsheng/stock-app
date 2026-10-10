"""初始化范围：默认全部；可只随机拉 N 只（抽样结果保存、续跑沿用、可重新抽、可随后补全）。"""
import json

from server import db, ingest, scanner, settings
from server.jobs import JobManager


def _bars_symbols(market):
    with db.market_db(market) as c:
        return {r[0] for r in c.execute("SELECT DISTINCT symbol FROM daily_bar")}


def test_random_sample_only_downloads_n_and_is_reusable(fresh_env):
    jm = JobManager()
    out = jm.init("CN", sample=40)
    with db.market_db("CN") as c:
        meta = json.loads(db.get_meta(c, "init_sample"))
        assert meta["n"] == 40 and len(meta["symbols"]) == 40 and out["sample"]["picked"] == 40
        assert db.get_meta(c, "init_complete") == "0", "抽样模式不算完整初始化（不会自动回补其余股票）"
        assert c.execute("SELECT COUNT(*) FROM securities").fetchone()[0] > 100, "名单仍是全部，只是历史只拉抽样的这批"
    assert _bars_symbols("CN") == set(meta["symbols"])
    # 再次点击同样的 N：沿用同一批，不换、不重复下载
    again = jm.init("CN", sample=40)
    assert again["history"]["requested"] == 0
    with db.market_db("CN") as c:
        assert json.loads(db.get_meta(c, "init_sample"))["symbols"] == meta["symbols"]
    # 重新抽一批：换一批并下载新的
    jm.init("CN", sample=40, resample=True)
    with db.market_db("CN") as c:
        new = json.loads(db.get_meta(c, "init_sample"))["symbols"]
    assert new != meta["symbols"] and set(new) <= _bars_symbols("CN")


def test_sample_then_full_completes_without_redownloading(fresh_env):
    jm = JobManager()
    jm.init("CN", sample=30)
    first = _bars_symbols("CN")
    with db.market_db("CN") as c:
        total_l1 = c.execute("SELECT COUNT(*) FROM securities WHERE in_l1=1 AND status!='delisted'").fetchone()[0]
    full = jm.init("CN", sample=0)                                  # 选「全部」补全
    assert full["history"]["requested"] >= total_l1 - 30 and full["history"]["requested"] <= total_l1 - 30 + 15, "只补没拉过的，已抽样的 30 只不重复下载"
    assert first <= _bars_symbols("CN") and len(_bars_symbols("CN")) > 100
    with db.market_db("CN") as c:
        assert db.get_meta(c, "init_sample") is None, "选「全部」后清除抽样记录"


def test_sample_mode_pipeline_still_scans_and_uses_default_setting(fresh_env):
    settings.save_user_config({"init": {"sample_size": 60}})          # 设置里记住「随机 60 只」后，默认就按它来
    settings.cfg()["datasource"]["cn"] = "demo"                       # save_user_config 会重载配置，重新指定演示数据源（绝不能在测试里联网）
    settings.cfg()["datasource"]["us"] = "demo"
    assert settings.cfg()["init"]["sample_size"] == 60
    JobManager().init("CN")                                          # 不传参数：按设置
    assert len(_bars_symbols("CN")) == 60
    r = scanner.run_scan("CN")
    assert r["official"] and r["summary"]["universe_l2"] > 0


def test_us_sample_skips_prefilter_and_limits_industry(fresh_env):
    out = JobManager().init("US", sample=25)
    assert len(_bars_symbols("US")) == 25 and "prefilter" not in out
    with db.market_db("US") as c:
        assert c.execute("SELECT COUNT(*) FROM industry_map").fetchone()[0] <= 25


def test_api_init_remember_writes_user_config(demo_env, monkeypatch):
    import warnings
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    from fastapi.testclient import TestClient
    from server import jobs, main
    seen = {}
    monkeypatch.setattr(jobs.manager, "start", lambda task, **kw: seen.update(task=task, **kw) or {"ok": True, "message": "ok"})
    cl = TestClient(main.app)
    assert cl.post("/api/jobs/init", json={"sample": 100, "remember": True}).json()["ok"]
    assert seen["sample"] == 100 and seen["task"] == "init"
    assert settings.cfg()["init"]["sample_size"] == 100
    cl.post("/api/jobs/init", json={"sample": 0, "remember": True})
    assert settings.cfg()["init"]["sample_size"] == 0
