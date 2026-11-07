"""各市场的「自动更新」开关（设置 → 通用设置 → jobs.auto_update）：默认只开 A 股；关掉的市场，后台调度不再自动补拉。"""
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

from server import jobs, market_calendar as mc, settings  # noqa: E402


@pytest.fixture
def client(demo_env):
    from server import main
    return TestClient(main.app)


def test_default_is_only_a_share_on(demo_env):
    assert settings.auto_update_enabled("CN") is True and settings.auto_update_enabled("US") is False
    assert settings.auto_update_enabled("us") is False                              # 大小写不敏感


def test_missing_config_falls_back_to_cn_on_us_off(demo_env, monkeypatch):
    """用户的 user_config.yaml 里没有这一项（老用户升级）：和默认一样，A 股开、美股关。"""
    cfg = settings.load_config()
    monkeypatch.setitem(cfg["jobs"], "auto_update", {})
    assert settings.auto_update_enabled("CN") is True and settings.auto_update_enabled("US") is False
    monkeypatch.delitem(cfg["jobs"], "auto_update")
    assert settings.auto_update_enabled("CN") is True and settings.auto_update_enabled("US") is False


def test_switch_roundtrips_through_the_settings_api(client):
    assert client.get("/api/settings").json()["jobs"]["auto_update"] == {"CN": True, "HK": False, "US": False}                # 默认只开 A 股；港股（只更新三个指数）也默认关
    r = client.put("/api/settings", json={"jobs": {"auto_update": {"US": True}}}).json()
    assert r["jobs"]["auto_update"] == {"CN": True, "HK": False, "US": True}          # 只改美股，A 股、港股不受影响
    assert settings.auto_update_enabled("US") is True
    r = client.put("/api/settings", json={"jobs": {"auto_update": {"CN": False}}}).json()
    assert r["jobs"]["auto_update"] == {"CN": False, "HK": False, "US": True} and settings.auto_update_enabled("CN") is False
    r = client.put("/api/settings", json={"jobs": {"auto_update": {"HK": True}}}).json()
    assert r["jobs"]["auto_update"]["HK"] is True and settings.auto_update_enabled("HK") is True and settings.auto_update_enabled("US") is True
    assert r["jobs"]["poll_interval_minutes"] == 10                                  # 其余 jobs 参数没被覆盖掉


def _stale_calendar(monkeypatch, started):
    """让调度认为「最近已收盘交易日」比数据新、数据已经可用：开着的市场就会触发补跑。"""
    monkeypatch.setattr(mc, "last_closed_trading_day", lambda conn, market, now=None: "2999-01-01")
    monkeypatch.setattr(jobs.JobManager, "_data_ready", staticmethod(lambda conn, market, day: True))
    mgr = jobs.JobManager()
    monkeypatch.setattr(mgr, "start", lambda task, market="CN", **kw: started.append((task, market)))
    return mgr


def test_scheduler_tick_skips_markets_with_auto_update_off(demo_env, monkeypatch):
    started = []
    mgr = _stale_calendar(monkeypatch, started)
    mgr.tick()
    assert started == [("catch_up", "CN")]                                           # 默认：A 股会自动补跑
    started.clear()
    settings.save_user_config({"jobs": {"auto_update": {"CN": False}}})
    mgr.tick()
    assert started == []                                                             # 关掉后不再自动补跑
    settings.save_user_config({"jobs": {"auto_update": {"CN": True}}})
    mgr.tick()
    assert started == [("catch_up", "CN")]                                           # 重新打开又恢复
