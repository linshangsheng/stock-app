"""存储（全量备份 gzip 压缩、只留 1 份、可恢复；清理只允许安全项）与短期止盈（个股 +10%、持仓体检提示、指数参考位）。"""
import gzip
import sqlite3

import pytest

from server import backup, selection, settings


def test_full_backup_is_compressed_and_restorable(demo_env):
    info = backup.run_backup(full=True, day="2026-01-05")
    froot = backup.full_dir() / "2026-01-05"
    assert (froot / "ashare.db.gz").exists() and not (froot / "ashare.db").exists()
    raw = backup.settings.data_dir() / "ashare.db"
    with gzip.open(froot / "ashare.db.gz", "rb") as f:
        assert f.read(16).startswith(b"SQLite format 3")
    n0 = sqlite3.connect(raw).execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0]
    out = backup.settings.data_dir() / "restored_check.db"          # 恢复路径同款解压，校验内容完整（运行中的库被占用，不在测试里覆盖）
    backup._gunzip_to(froot / "ashare.db.gz", out)
    assert sqlite3.connect(out).execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0] == n0
    assert "gzip" in info["files"]["ashare.db"]


def test_only_newest_full_backup_kept(demo_env):
    backup.run_backup(full=True, day="2026-01-05")
    backup.run_backup(full=True, day="2026-01-12")
    days = sorted(p.name for p in backup.full_dir().iterdir() if p.is_dir())
    assert days == ["2026-01-12"]


def test_storage_clean_refuses_core_data_and_deletes_demo(demo_env):
    for key in ("ashare", "portfolio", "us", "nonexistent"):
        with pytest.raises(ValueError):
            backup.storage_clean(key)
    demo = backup.settings.data_dir() / "demo"
    demo.mkdir(exist_ok=True)
    (demo / "x.db").write_bytes(b"0" * 1000)
    r = backup.storage_clean("demo")
    assert r["freed_bytes"] >= 1000 and not demo.exists()


def test_operation_plan_has_take_profit():
    o = selection.operation_plan(10.0, 9.0, "main", 100000, 0.005)
    assert o["take_profit_pct"] == settings.cfg()["exits"]["take_profit_pct"] == 0.10
    assert o["take_profit"] == 11.0


def test_index_view_has_reference_take_profit(demo_env):
    from server import db, market_view as mv
    with db.market_db("CN") as c:
        mv.ensure_breadth(c, "CN")
    mv._cache.clear()
    d = mv.build("CN")
    w = d["indices"][0]["washout"]
    assert [x["pct"] for x in w["ref_take_profit"]] == [0.05, 0.10]
