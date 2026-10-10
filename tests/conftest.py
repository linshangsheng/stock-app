"""测试夹具：每个测试使用隔离的数据目录（STOCK_DATA_DIR），演示数据只生成一次（session 级）。"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _reload_settings(demo: bool):
    from server import settings
    settings.load_config(reload=True)
    if demo:
        settings.cfg()["datasource"]["cn"] = "demo"
        settings.cfg()["datasource"]["us"] = "demo"
    # 机制测试（成交规则 / 无未来函数 / 回填）需要足够多的信号：合成数据上启用全部经典形态。
    # 生产默认只启用回测证据支持的形态（config.yaml setups.enabled），与这里无关。
    settings.cfg()["setups"]["enabled"] = ["breakout", "pullback", "vcp"]
    return settings


@pytest.fixture
def fresh_env(tmp_path, monkeypatch):
    """空的数据目录 + 演示数据源配置。"""
    monkeypatch.setenv("STOCK_DATA_DIR", str(tmp_path / "data"))
    s = _reload_settings(True)
    (tmp_path / "data").mkdir(exist_ok=True)
    yield tmp_path / "data"
    s.load_config(reload=True)


@pytest.fixture(scope="session")
def demo_template(tmp_path_factory):
    """生成一次演示数据（160 只 × 约 6 年），其余测试复制使用。"""
    d = tmp_path_factory.mktemp("demo_template") / "data"
    d.mkdir()
    old = os.environ.get("STOCK_DATA_DIR")
    os.environ["STOCK_DATA_DIR"] = str(d)
    s = _reload_settings(True)
    from server import db, ingest, universe
    from server.datasource_cn import get_source
    src = get_source()
    with db.market_db("CN") as c:
        ingest.ensure_calendar(c, src)
        ingest.refresh_securities(c, src)
        ingest.refresh_industry(c, src)
        universe.apply_l1(c)
        ingest.init_history(c, src)
        ingest.refresh_indices(c, src)
        db.set_meta(c, "data_asof", c.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0])
    # 关闭 WAL 以便复制
    import sqlite3
    for name in ("ashare.db",):
        con = sqlite3.connect(d / name)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
    if old is None:
        os.environ.pop("STOCK_DATA_DIR", None)
    else:
        os.environ["STOCK_DATA_DIR"] = old
    return d


@pytest.fixture
def demo_env(demo_template, tmp_path, monkeypatch):
    """每个测试得到演示数据的独立副本（可随意写入持仓 / 扫描记录而互不影响）。"""
    d = tmp_path / "data"
    shutil.copytree(demo_template, d, ignore=shutil.ignore_patterns("*-wal", "*-shm", "portfolio.db*", "backups"))
    monkeypatch.setenv("STOCK_DATA_DIR", str(d))
    s = _reload_settings(True)
    yield d
    s.load_config(reload=True)


@pytest.fixture(scope="session")
def us_template(tmp_path_factory):
    """美股演示数据（200 只 × 约 6 年，含拆股），其余测试复制使用。"""
    d = tmp_path_factory.mktemp("us_template") / "data"
    d.mkdir()
    old = os.environ.get("STOCK_DATA_DIR")
    os.environ["STOCK_DATA_DIR"] = str(d)
    _reload_settings(True)
    from server.jobs import JobManager
    JobManager().init("US")
    import sqlite3
    con = sqlite3.connect(d / "us.db")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    if old is None:
        os.environ.pop("STOCK_DATA_DIR", None)
    else:
        os.environ["STOCK_DATA_DIR"] = old
    return d


@pytest.fixture
def us_env(us_template, tmp_path, monkeypatch):
    d = tmp_path / "data"
    shutil.copytree(us_template, d, ignore=shutil.ignore_patterns("*-wal", "*-shm", "portfolio.db*", "backups*"))
    monkeypatch.setenv("STOCK_DATA_DIR", str(d))
    s = _reload_settings(True)
    yield d
    s.load_config(reload=True)
