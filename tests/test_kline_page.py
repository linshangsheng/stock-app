"""「K线」页：拼音搜索（代码 / 中文名 / 首字母 / 全拼）与页面自己的「我的股票」名单。"""
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

from server import db, pinyin_search  # noqa: E402


@pytest.fixture
def client(demo_env):
    from server import main
    return TestClient(main.app)


def test_spell_initials_and_full_pinyin():
    assert pinyin_search.available()
    assert pinyin_search.spell("格力电器") == ("gldq", "gelidianqi")
    assert pinyin_search.spell("招商银行")[0] == "zsyh"                    # 「行」在词里读 háng，不能被拆成 xing
    assert pinyin_search.spell("*ST 凯迪")[0] == "stkd"                  # 名称里的英文字母保留，符号去掉
    assert pinyin_search.spell("") == ("", "")


def _insert(name, symbol, in_l1=1):
    with db.market_db("CN") as c:
        c.execute("INSERT OR REPLACE INTO securities(symbol,name,sec_type,board,status,in_l1) VALUES(?,?,?,?,?,?)", (symbol, name, "stock", "main", "active", in_l1))
        c.commit()


def test_search_by_name_code_initials_and_pinyin(client):
    _insert("格力电器", "sz.000651")
    _insert("国联电气", "sz.000999")                                     # 首字母也是 gldq 开头吗？—— 用来确认同一个缩写能搜出多只
    for q in ("格力电器", "000651", "sz.000651", "gldq", "GLDQ", "gelidianqi", "geli", "力电"):
        r = client.get("/api/search", params={"q": q}).json()
        assert r["pinyin"] is True and r["items"][0]["symbol"] == "sz.000651", q
    r = client.get("/api/search", params={"q": "gldq"}).json()["items"]
    assert [x["code"] for x in r][:1] == ["000651"] and all("name" in x for x in r)
    assert client.get("/api/search", params={"q": "zzzzqqqq"}).json()["items"] == []
    assert client.get("/api/search", params={"q": " "}).json()["items"] == []


def test_search_ranking_exact_code_first(client):
    _insert("甲乙丙丁", "sz.000111")
    _insert("某某某某", "sz.001110")
    r = client.get("/api/search", params={"q": "000111"}).json()["items"]
    assert r[0]["symbol"] == "sz.000111"                                # 代码完全相同的排最前，只是包含的排后面


def test_search_cache_follows_security_list(client):
    assert client.get("/api/search", params={"q": "xjdr"}).json()["items"] == []
    _insert("新进的人", "sz.000777")
    assert client.get("/api/search", params={"q": "xjdr"}).json()["items"][0]["symbol"] == "sz.000777"        # 名单数量变了，缓存自动重算


def test_search_falls_back_to_names_without_pypinyin(client, monkeypatch):
    _insert("格力电器", "sz.000651")
    monkeypatch.setattr(pinyin_search, "lazy_pinyin", None)
    pinyin_search._cache.clear()
    r = client.get("/api/search", params={"q": "格力"}).json()
    assert r["pinyin"] is False and r["items"][0]["symbol"] == "sz.000651"
    assert client.get("/api/search", params={"q": "gldq"}).json()["items"] == []      # 没装就搜不到拼音，但不报错
    pinyin_search._cache.clear()


def test_kline_list_add_remove_and_isolation(client):
    syms = [x["symbol"] for x in client.get("/api/search", params={"q": "演示股票0"}).json()["items"][:3]]
    assert client.get("/api/kline/list").json()["items"] == []
    r = client.put("/api/kline/list", json={"add": [syms[0], syms[1], syms[0], "xx.999999"]}).json()        # 重复的、库里没有的都忽略
    assert sorted(i["symbol"] for i in r["items"]) == sorted(syms[:2])
    it = r["items"][0]
    assert it["name"].startswith("演示股票") and it["code"] and it["quote"]["close"] > 0
    assert client.get("/api/watchlist").json()["items"] == []            # 和自选股是两份名单
    r = client.put("/api/kline/list", json={"remove": [syms[0]]}).json()
    assert [i["symbol"] for i in r["items"]] == [syms[1]]
    assert client.get("/api/kline/list", params={"market": "US"}).json()["items"] == []                  # 按市场分开存
    assert client.put("/api/kline/list", json={"remove": [syms[1]]}).json()["items"] == []
