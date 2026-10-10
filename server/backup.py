"""备份与恢复（3.26.6）：不可重建数据每日备份。
  * 每日：portfolio.db（自选 / 持仓 / 交易日志）+ 行情库中的不可重建部分 essential.db
    （scan_runs / scan_results / scan_outcomes / backtest_runs / securities / corp_actions / 已退市股票日线）+ config.yaml
  * 每周（磁盘允许）：行情库全量
  * 机制：SQLite 在线备份 API（WAL 下一致性快照）；保留策略默认 近 14 天日备 + 近 12 个月月备；目录可配置到网盘 / 外接盘。"""
from __future__ import annotations

import gzip
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


def _gzip_file(src: Path, level: int = 1) -> Path:
    """原地压缩为 .gz 并删除原文件（级别 1：速度约 100 MB/s，体积约原来的 31%）。"""
    dst = src.with_name(src.name + ".gz")
    with open(src, "rb") as fi, gzip.open(dst, "wb", compresslevel=level) as fo:
        shutil.copyfileobj(fi, fo, length=8 * 1024 * 1024)
    src.unlink()
    return dst


def _gunzip_to(src: Path, dst: Path) -> None:
    with gzip.open(src, "rb") as fi, open(dst, "wb") as fo:
        shutil.copyfileobj(fi, fo, length=8 * 1024 * 1024)


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
        _gzip_file(root / f"essential_{name}")
        if full is None:
            full_ = _need_weekly_full(name)
        else:
            full_ = full
        if full_:
            froot = full_dir() / day
            froot.mkdir(parents=True, exist_ok=True)
            _online_backup(src, froot / name)
            _gzip_file(froot / name)
            info["files"][name] = f"full -> {froot}（gzip）"
    cfgp = settings.CONFIG_PATH
    if cfgp.exists():
        shutil.copy2(cfgp, root / "config.yaml")
    info["pruned"] = prune()
    return info


def _need_weekly_full(name: str) -> bool:
    for d in sorted((p for p in full_dir().iterdir() if p.is_dir()), reverse=True):
        if (d / name).exists() or (d / (name + ".gz")).exists():
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
    for root, daily, monthly in ((backup_dir(), cfg["keep_daily"], cfg["keep_monthly"]), (full_dir(), int(cfg.get("keep_full", 1)), 0)):
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


# ---- 存储空间：明细与（用户点按钮才执行的）清理 ----------------------------------------

def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


def storage_report() -> dict:
    """数据目录各部分的大小与用途；cleanable = 可以安全清理（可重新生成 / 已被新位置取代）。"""
    dd = settings.data_dir()
    items = []

    def add(key, path: Path, title, desc, cleanable=False, action=None):
        if path.exists():
            items.append({"key": key, "path": str(path), "title": title, "desc": desc, "bytes": _size(path),
                          "cleanable": cleanable, "action": action})

    add("ashare", dd / "ashare.db", "A 股行情库", "10 年日线、指数、扫描与回测记录。核心数据，删了要重新初始化（6~9 小时）")
    add("us", dd / "us.db", "美股行情库", "不用美股可以不管它；删了要重新初始化")
    add("portfolio", dd / "portfolio.db", "个人数据", "自选、持仓、交易日志、账户。最重要，每天自动备份")
    fd = full_dir()
    fulls = sorted((p for p in fd.iterdir() if p.is_dir()), reverse=True) if fd.exists() else []
    for i, p in enumerate(fulls):
        raw = [f.name for f in p.iterdir() if f.suffix == ".db"]
        add(f"full:{p.name}", p, f"行情库全量备份 {p.name}", ("最新一份，留着应急" if i == 0 else "较早的一份，可以删") +
            ("；未压缩，点「压缩」可缩到约 1/3" if raw else "（已压缩）"),
            cleanable=i > 0, action="delete" if i > 0 else ("compress" if raw else None))
    bd = backup_dir()
    local = dd / "backups"
    if local.exists() and local.resolve() != bd.resolve():
        add("old_local_backups", local, "旧的本地日备份", f"每日备份已改存到 {bd}，这里是改之前留下的旧备份，可以删", cleanable=True, action="delete")
    add("daily_backups", bd, "每日备份", f"位于 {bd}；保留近 {settings.cfg()['backup']['keep_daily']} 天 + 每月一份，自动清理")
    add("demo", dd / "demo", "演示数据", "合成的假数据，只在「演示模式」用；删了需要时会自动重新生成", cleanable=True, action="delete")
    add("log", dd / "server.log", "运行日志", "排查问题用；新版本不再逐条记录页面请求，增长很慢")
    for f in dd.glob("*.before_restore"):
        add(f"before_restore:{f.name}", f, f"恢复前的旧文件 {f.name}", "上次「恢复备份」前自动另存的旧文件，确认恢复无误后可删", cleanable=True, action="delete")
    total = _size(dd) + (_size(bd) if not str(bd.resolve()).startswith(str(dd.resolve())) else 0)
    return {"data_dir": str(dd), "items": items, "total_bytes": total}


def storage_clean(key: str) -> dict:
    """只允许清理 storage_report 标记为可清理 / 可压缩的项（防误删核心数据）。"""
    rep = {i["key"]: i for i in storage_report()["items"]}
    it = rep.get(key)
    if not it or not it["action"]:
        raise ValueError("这一项不能清理")
    p = Path(it["path"])
    before = it["bytes"]
    if it["action"] == "compress":
        for f in list(p.glob("*.db")):
            _gzip_file(f)
    else:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    return {"key": key, "freed_bytes": before - _size(p)}


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
        gz = root / (name + ".gz")
        if not src.exists() and not gz.exists():
            raise FileNotFoundError(f"备份中没有 {name}（{src}）")
        dst = dd / name
        if dst.exists():
            shutil.copy2(dst, dd / (name + ".before_restore"))
        for ext in ("-wal", "-shm"):
            (dd / (name + ext)).unlink(missing_ok=True)
        if src.exists():
            shutil.copy2(src, dst)
        else:
            _gunzip_to(gz, dst)
        done[name] = _count_portfolio(dst) if name == "portfolio.db" else "restored"
    return done
