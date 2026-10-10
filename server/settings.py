"""配置加载：集中读取 config.yaml；环境变量 STOCK_DATA_DIR 可覆盖数据目录（测试 / 演示用）。"""
from __future__ import annotations

import contextlib
import contextvars
import copy
import hashlib
import json
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("STOCK_CONFIG", ROOT / "server" / "config.yaml"))

_cache: dict | None = None


# 设置页允许修改的配置路径（白名单）；其余参数请直接编辑 config.yaml
USER_EDITABLE = {
    "costs": None, "execution": None, "exits": None, "portfolio": None, "funnel": None, "setups": None,
    "universe": None, "gate": None, "regime": None, "backup": None, "jobs": None, "features": None, "markets": None, "init": None, "market_view": None, "allocation": None, "factor_portfolio": None, "key_ma": None,
}


def user_config_path() -> Path:
    return data_dir() / "user_config.yaml"


def _base_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_config(reload: bool = False) -> dict:
    global _cache
    if _cache is None or reload:
        base = _base_config()
        up = user_config_path()
        if up.exists():
            with open(up, "r", encoding="utf-8") as f:
                base = deep_merge(base, yaml.safe_load(f) or {})
        _cache = base
        _merged_cache.clear()
    return _cache


def save_user_config(patch: dict) -> dict:
    """把设置页修改写入 data/user_config.yaml（覆盖层），并重新加载。仅允许白名单顶层键。"""
    bad = [k for k in patch if k not in USER_EDITABLE]
    if bad:
        raise ValueError(f"不允许修改的配置项：{bad}")
    up = user_config_path()
    cur = {}
    if up.exists():
        with open(up, "r", encoding="utf-8") as f:
            cur = yaml.safe_load(f) or {}
    cur = deep_merge(cur, patch)
    with open(up, "w", encoding="utf-8") as f:
        yaml.safe_dump(cur, f, allow_unicode=True, sort_keys=False)
    return load_config(reload=True)


# ---- 按市场的配置视图（需求书 2.1：A股 / 美股参数各自独立）----
# config.yaml 的 markets.<MARKET> 是对基础配置的覆盖层；cfg() 按「当前市场上下文」返回合并后的配置。
# 当前市场用 ContextVar 传递：HTTP 请求由中间件按 market 参数设置，后台线程 / 任务显式 with market_ctx(...)。
_market_var: contextvars.ContextVar[str] = contextvars.ContextVar("market", default="CN")
_merged_cache: dict[tuple[int, str], dict] = {}


def current_market() -> str:
    return _market_var.get()


@contextlib.contextmanager
def market_ctx(market: str):
    tok = _market_var.set((market or "CN").upper())
    try:
        yield
    finally:
        _market_var.reset(tok)


def set_market(market: str):
    return _market_var.set((market or "CN").upper())


def cfg() -> dict:
    base = load_config()
    m = _market_var.get()
    if m == "CN":
        return base
    over = (base.get("markets") or {}).get(m)
    if not over:
        return base
    key = (id(base), m)
    if key not in _merged_cache:
        _merged_cache.clear()
        _merged_cache[key] = deep_merge(base, over)
    return _merged_cache[key]


def base_cfg() -> dict:
    return load_config()


def deep_merge(base: dict, override: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def data_dir() -> Path:
    env = os.environ.get("STOCK_DATA_DIR")
    p = Path(env) if env else ROOT / _base_config().get("data_dir", "data")
    if not p.is_absolute():
        p = ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_hash(obj) -> str:
    """配置快照哈希：同一 run 可复现（1.6 可复现）。"""
    s = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def git_commit() -> str:
    try:
        import subprocess

        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"
