"""股票搜索：代码 / 中文名称 / 拼音首字母（gldq = 格力电器）/ 全拼（geli）。
拼音靠 pypinyin 现算（名单约 5000 只，算一遍不到 1 秒），按市场缓存在内存里；名单变了（数量变化）自动重算。
没装 pypinyin 时退回到只按代码和名称搜索（available() 为 False，前端会提示），不影响其他功能。"""
from __future__ import annotations

import threading

from . import settings

try:
    from pypinyin import Style, lazy_pinyin
except ImportError:  # noqa: BLE001 - 老用户升级后还没装：退回名称搜索
    lazy_pinyin = None

_cache: dict = {}
_lock = threading.Lock()


def available() -> bool:
    return lazy_pinyin is not None


def _keep_alnum(s: str):
    return [c.lower() for c in s if c.isalnum()]            # 名称里的英文 / 数字（*ST、A、B）原样保留，符号丢掉


def spell(name: str) -> tuple[str, str]:
    """(首字母串, 全拼串)，全小写、无空格。例：格力电器 -> ('gldq', 'geilidianqi')；'*ST 凯迪' 中的 ST 保持为 st。"""
    if lazy_pinyin is None or not name:
        return "", ""
    ini = "".join(lazy_pinyin(name, style=Style.FIRST_LETTER, errors=_keep_alnum))
    full = "".join(lazy_pinyin(name, style=Style.NORMAL, errors=_keep_alnum))
    return ini, full


def _index(conn, market: str) -> list[dict]:
    # 名单有没有变：只数条数不够（改名、换了数据目录条数可能不变），再加名称总长度和数据目录一起比
    n = (settings.data_dir().as_posix(), *conn.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(name)),0) FROM securities WHERE sec_type='stock'").fetchone())
    with _lock:
        hit = _cache.get(market)
    if hit and hit[0] == n:
        return hit[1]
    rows = conn.execute("SELECT symbol,name,board,status,in_l1 FROM securities WHERE sec_type='stock'").fetchall()
    out = []
    for r in rows:
        ini, full = spell(r["name"] or "")
        out.append({"symbol": r["symbol"], "name": r["name"], "board": r["board"], "status": r["status"], "in_l1": r["in_l1"] or 0,
                    "code": r["symbol"].split(".")[-1].lower(), "name_l": (r["name"] or "").lower().replace(" ", ""), "ini": ini, "full": full})
    with _lock:
        _cache[market] = (n, out)
    return out


def _rank(it: dict, q: str) -> int | None:
    """越小越靠前；None = 不匹配。"""
    if it["code"] == q or it["symbol"].lower() == q:
        return 0
    if it["name_l"] == q:
        return 1
    if it["code"].startswith(q):
        return 2
    if it["name_l"].startswith(q):
        return 3
    if it["ini"].startswith(q):
        return 4
    if it["full"].startswith(q):
        return 5
    if q in it["name_l"]:
        return 6
    if q in it["ini"]:
        return 7
    if q in it["code"]:
        return 8
    if q in it["full"]:
        return 9
    return None


def search(conn, market: str, q: str, limit: int = 20) -> list[dict]:
    """返回 securities 行（symbol / name / board / status），排序：代码精确 > 名称 > 拼音首字母 > 全拼；同档次先主板（in_l1）再按代码。"""
    q = q.strip().lower().replace(" ", "")
    if not q:
        return []
    hits = []
    for it in _index(conn, market):
        r = _rank(it, q)
        if r is not None:
            hits.append((r, -it["in_l1"], it["symbol"], it))
    hits.sort(key=lambda t: t[:3])
    return [{k: it[k] for k in ("symbol", "name", "board", "status")} for *_, it in hits[:limit]]
