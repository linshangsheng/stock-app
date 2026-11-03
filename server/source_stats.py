"""上游数据源的请求统计（数据健康面板用）：每个源按天记「成功 / 失败」次数、上次成功时间、最近几条报错。
只统计经过限速层（throttle.Throttle.call）的真实请求；熔断中被直接拒绝的不算（没有发出去）。
落盘到 data/source_stats.json（最多每 20 秒写一次，后端重启前会主动写一次），重启后不清零；只保留最近 14 天。"""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta

from . import settings

KEEP_DAYS = 14
FLUSH_EVERY = 20.0          # 秒
MAX_ERRORS = 5

_lock = threading.Lock()
_state: dict = {}
_path_loaded: str | None = None
_dirty = False
_last_flush = 0.0


def _path():
    return settings.data_dir() / "source_stats.json"


def _ensure_loaded() -> None:
    """数据目录变了（测试 / 换配置）就重新读，不把别处的统计串进来。"""
    global _state, _path_loaded, _dirty
    p = str(_path())
    if _path_loaded == p:
        return
    _state, _dirty = {"sources": {}}, False
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("sources"), dict):
            _state = d
    except (OSError, ValueError):
        pass
    _path_loaded = p


def _src(name: str) -> dict:
    return _state["sources"].setdefault(name, {"days": {}, "last_ok": None, "last_err": None, "errors": []})


def _prune(s: dict) -> None:
    cut = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    for d in [d for d in s["days"] if d < cut]:
        del s["days"][d]


def record(name: str, ok: bool, msg: str | None = None) -> None:
    global _dirty
    try:
        with _lock:
            _ensure_loaded()
            s = _src(name)
            today = date.today().isoformat()
            ok_err = s["days"].setdefault(today, [0, 0])
            now = datetime.now().isoformat(timespec="seconds")
            if ok:
                ok_err[0] += 1
                s["last_ok"] = now
            else:
                ok_err[1] += 1
                s["last_err"] = now
                s["errors"] = ([{"at": now, "msg": (msg or "")[:200]}] + s["errors"])[:MAX_ERRORS]
            _dirty = True
            _flush_locked(force=False)
    except Exception:  # noqa: BLE001 - 统计失败绝不能影响真正的请求
        pass


def _flush_locked(force: bool) -> None:
    global _dirty, _last_flush
    if not _dirty or (not force and time.monotonic() - _last_flush < FLUSH_EVERY):
        return
    try:
        for s in _state["sources"].values():
            _prune(s)
        _path().write_text(json.dumps(_state, ensure_ascii=False), encoding="utf-8")
        _dirty, _last_flush = False, time.monotonic()
    except OSError:
        _last_flush = time.monotonic()          # 写不进去也别每次都重试


def flush() -> None:
    """后端重启 / 退出前调用，把没写盘的统计落下来。"""
    with _lock:
        _ensure_loaded()
        _flush_locked(force=True)


def summary(name: str, days: int = 7) -> dict:
    """最近 days 天（含今天）的成功 / 失败次数与成功率；没有请求时 rate 为 None。"""
    with _lock:
        _ensure_loaded()
        s = _state["sources"].get(name)
        if not s:
            return {"ok": 0, "err": 0, "rate": None, "last_ok": None, "last_err": None, "errors": [], "today": [0, 0]}
        cut = (date.today() - timedelta(days=days - 1)).isoformat()
        ok = sum(v[0] for d, v in s["days"].items() if d >= cut)
        err = sum(v[1] for d, v in s["days"].items() if d >= cut)
        n = ok + err
        return {"ok": ok, "err": err, "rate": (ok / n) if n else None, "last_ok": s.get("last_ok"), "last_err": s.get("last_err"),
                "errors": list(s.get("errors") or []), "today": list(s["days"].get(date.today().isoformat(), [0, 0]))}
