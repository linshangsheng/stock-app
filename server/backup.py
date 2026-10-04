"""备份与恢复（3.26.6）：不可重建数据每日备份。
  * 每日：portfolio.db（自选 / 持仓 / 交易日志）+ 行情库中的不可重建部分 essential.db
    （scan_runs / scan_results / scan_outcomes / backtest_runs / securities / corp_actions / 已退市股票日线）+ config.yaml
  * 每周（磁盘允许）：行情库全量
  * 机制：SQLite 在线备份 API（WAL 下一致性快照）；保留策略默认 近 14 天日备 + 近 12 个月月备；目录可配置到网盘 / 外接盘。"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db, settings


def _resolve(key: str, default: str) -> Path:
    p = Path(settings.cfg()["backup"].get(key) or default)
    if not p.is_absolute():
        p = settings.data_dir() / p                      # 相对路径相对于数据目录（演示 / 真实数据各自独立）
    p.mkdir(parents=True, exist_ok=True)
    return p


def backup_dir() -> Path:
    """每日备份目录：个人库 + 不可重建数据（体积小，可放网盘同步目录）。"""
    return _resolve("dir", "backups")


def full_dir() -> Path:
    """行情库全量备份目录（体积大，默认留在本机；放网盘会占大量空间 / 流量）。"""
    return _resolve("full_dir", "backups_full")


def _online_backup(src_path: Path, dst_path: Path) -> None:
    if dst_path.exists():
        dst_path.unlink()
    src = sqlite3.connect(str(src_path))
    dst = sqlite3.connect(str(dst_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _essential(src_path: Path, dst_path: Path) -> dict:
    """行情库中不可重建的部分：扫描记录、回测记录、证券主表、拆股 / 除权事件、已退市股票历史。"""
    if dst_path.exists():
        dst_path.unlink()
    dst = sqlite3.connect(str(dst_path))
    dst.execute("ATTACH DATABASE ? AS src", (str(src_path),))
    counts = {}
    for t in ("scan_runs", "scan_results", "scan_outcomes", "backtest_runs", "securities", "corp_actions", "events", "industry_map"):
        dst.execute(f"CREATE TABLE {t} AS SELECT * FROM src.{t}")
        counts[t] = dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    dst.execute("CREATE TABLE daily_bar AS SELECT * FROM src.daily_bar WHERE symbol IN "
                "(SELECT symbol FROM src.securities WHERE status='delisted')")
    counts["daily_bar(delisted)"] = dst.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0]
    dst.commit()
    dst.execute("DETACH DATABASE src")
    dst.close()
    return counts


def run_backup(full: bool | None = None, day: str | None = None) -> dict:
    day = day or date.today().isoformat()
    root = backup_dir() / day
    root.mkdir(parents=True, exist_ok=True)
    info: dict = {"dir": str(root), "files": {}}
    dd = settings.data_dir()
    pf = dd / "portfolio.db"
    if pf.exists():
        _online_backup(pf, root / "portfolio.db")
        info["files"]["portfolio.db"] = _count_portfolio(root / "portfolio.db")
    for m, name in db.MARKET_DB.items():
        src = dd / name
        if not src.exists():
            continue
        info["files"][f"essential_{name}"] = _essential(src, root / f"essential_{name}")
        if full is None:
            full_ = _need_weekly_full(name)
        else:
            full_ = full
        if full_:
            froot = full_dir() / day
            froot.mkdir(parents=True, exist_ok=True)
            _online_backup(src, froot / name)
            info["files"][name] = f"full -> {froot}"
    cfgp = settings.CONFIG_PATH
    if cfgp.exists():
        shutil.copy2(cfgp, root / "config.yaml")
    info["pruned"] = prune()
    return info


def _need_weekly_full(name: str) -> bool:
    for d in sorted((p for p in full_dir().iterdir() if p.is_dir()), reverse=True):
        if (d / name).exists():
            try:
                return (date.today() - date.fromisoformat(d.name)).days >= 7
            except ValueError:
                continue
    return True


def _count_portfolio(path: Path) -> dict:
    c = sqlite3.connect(str(path))
    try:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("watchlist", "account", "positions", "trades", "alerts")}
    finally:
        c.close()


def prune() -> list[str]:
    """保留策略：最近 keep_daily 天的日备 + 最近 keep_monthly 个月每月最早的一份；全量备份目录同样处理（全量只保留最近 3 份）。"""
    cfg = settings.cfg()["backup"]
    removed = []
    for root, daily, monthly in ((backup_dir(), cfg["keep_daily"], cfg["keep_monthly"]), (full_dir(), 3, 0)):
        dirs = []
        for p in root.iterdir():
            if p.is_dir():
                try:
                    dirs.append((date.fromisoformat(p.name), p))
                except ValueError:
                    pass
        dirs.sort(reverse=True)
        keep: set[Path] = {p for _, p in dirs[:daily]}
        by_month: dict[str, tuple[date, Path]] = {}
        for d, p in dirs:
            k = d.strftime("%Y-%m")
            if k not in by_month or d < by_month[k][0]:
                by_month[k] = (d, p)
        for k in sorted(by_month, reverse=True)[:monthly]:
            keep.add(by_month[k][1])
        for _, p in dirs:
            if p not in keep:
                shutil.rmtree(p, ignore_errors=True)
                removed.append(p.name)
    return removed


def list_backups() -> list[dict]:
    out = []
    for p in sorted((p for p in backup_dir().iterdir() if p.is_dir()), reverse=True):
        files = {f.name: f.stat().st_size for f in p.iterdir() if f.is_file()}
        out.append({"date": p.name, "files": files, "size_mb": round(sum(files.values()) / 1e6, 2)})
    return out


def backup_status() -> dict:
    items = list_backups()
    last = items[0]["date"] if items else None
    age = (date.today() - date.fromisoformat(last)).days if last else None
    return {"dir": str(backup_dir()), "full_dir": str(full_dir()), "last": last, "age_days": age, "count": len(items),
            "stale": age is None or age > 1, "items": items[:20]}


def restore(day: str, what: str = "portfolio") -> dict:
    """从备份恢复（须停止后端后执行；恢复前自动把现有文件另存为 .before_restore）。
    恢复完成后校验记录数（恢复演练：M1 完成时做一次，此后每季度一次）。"""
    root = (backup_dir() if what == "portfolio" else full_dir()) / day
    dd = settings.data_dir()
    done = {}
    targets = {"portfolio": ["portfolio.db"], "ashare": ["ashare.db"], "us": ["us.db"]}[what]
    for name in targets:
        src = root / name
        if not src.exists():
            raise FileNotFoundError(f"备份中没有 {name}（{src}）")
        dst = dd / name
        if dst.exists():
            shutil.copy2(dst, dd / (name + ".before_restore"))
        for ext in ("-wal", "-shm"):
            (dd / (name + ext)).unlink(missing_ok=True)
        shutil.copy2(src, dst)
        done[name] = _count_portfolio(dst) if name == "portfolio.db" else "restored"
    return done
