"""前端冒烟测试：启动演示数据的服务，用本机已有的 Edge / Chrome（无头模式，不需要安装任何东西）把每个页面真的打开一遍。
拦的是静态检查看不到的事：某个 JS 文件语法错 / 重复声明变量 → 整个页面一片空白（这类错误只有在浏览器里才会出现）、
页面渲染时抛异常、请求接口 4xx / 5xx。每个页面检查：页面标记存在、没有「页面加载失败」、浏览器控制台没有报错。
找不到浏览器，或设置了环境变量 STOCK_SKIP_BROWSER=1 时跳过。"""
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ROUTES = ["screener", "watchlist", "portfolio", "market", "kline", "market/backtest", "market/backtest/score", "settings", "settings/data", "settings/about", "settings/upgrade"]
NOISE = ("chrome-extension://", "edge://", "ProtocolLaunch", "edge_", "QQBrowser")        # 浏览器自己 / 扩展的日志，与页面无关


def _find_browser() -> str | None:
    if os.environ.get("STOCK_SKIP_BROWSER") == "1":
        return None
    if os.environ.get("STOCK_BROWSER") and Path(os.environ["STOCK_BROWSER"]).exists():
        return os.environ["STOCK_BROWSER"]
    cands = []
    for base in filter(None, (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA"))):
        cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe", Path(base) / "Google/Chrome/Application/chrome.exe"]
    cands += [Path(p) for p in (shutil.which(n) for n in ("msedge", "google-chrome", "chromium", "chromium-browser")) if p]
    return next((str(p) for p in cands if p.exists()), None)


BROWSER = _find_browser()
pytestmark = pytest.mark.skipif(not BROWSER, reason="没有找到 Edge / Chrome（或设置了 STOCK_SKIP_BROWSER=1）")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(extra_env):
    port = _free_port()
    env = {**os.environ, "PORT": str(port), **extra_env}
    proc = subprocess.Popen([sys.executable, "-m", "server.cli", "serve", "--demo"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(300):                                   # 第一次要生成演示数据，给 5 分钟
            if proc.poll() is not None:
                pytest.fail("演示服务启动失败")
            try:
                urllib.request.urlopen(url + "/api/ping", timeout=2).read()
                break
            except OSError:
                time.sleep(1)
        else:
            pytest.fail("演示服务 5 分钟内没有起来")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def base():
    """页面测试用：关掉登录（STOCK_AUTH=off，只在监听本机时有效），直接把每个页面打开一遍。"""
    yield from _serve({"STOCK_AUTH": "off"})


@pytest.fixture(scope="module")
def base_login():
    """开着登录的服务：没登录时页面应该停在登录页。"""
    yield from _serve({"STOCK_AUTH": ""})


def _visit(url: str, tmp: Path, wait_ms: int = 9000) -> tuple[str, list[str]]:
    """无头打开页面，等脚本跑完（虚拟时间），返回 (渲染后的 DOM, 页面控制台里的错误行)。"""
    r = subprocess.run([BROWSER, "--headless=new", "--disable-gpu", "--no-sandbox", f"--user-data-dir={tmp}", "--enable-logging=stderr", "--v=0",
                        f"--virtual-time-budget={wait_ms}", "--dump-dom", url], capture_output=True, timeout=180)
    dom = r.stdout.decode("utf-8", "replace")
    errs = []
    for line in r.stderr.decode("utf-8", "replace").splitlines():
        if "CONSOLE" not in line or any(n in line for n in NOISE):
            continue
        low = line.lower()
        if "uncaught" in low or "error" in low or "failed to load" in low or "failed to fetch" in low:
            errs.append(line.strip()[:300])
    return dom, errs


def test_harness_actually_sees_console_errors(tmp_path):
    """自检：故意抛一个未捕获异常，确认这套办法抓得到——否则下面的测试会「假通过」。（只打 console.error('文字') 抓不到，
    但页面自己的出错路径会抛异常或显示「页面加载失败」，这两种都查得到。）"""
    dom, errs = _visit("data:text/html,<body>x<script>throw new Error('boom-2')</script>", tmp_path / "p", 3000)
    assert any("boom-2" in e for e in errs), errs


@pytest.mark.parametrize("route", ROUTES)
def test_page_opens_without_errors(base, tmp_path, route):
    dom, errs = _visit(f"{base}/#/{route}", tmp_path / "p")
    name = route.split("/")[0]
    assert f'data-view="{name}"' in dom, f"{route}：页面没有挂载（整个页面可能因为某个 JS 文件出错而空白）"
    assert "页面加载失败" not in dom, f"{route}：显示了「页面加载失败」"
    assert not errs, f"{route}：浏览器控制台有报错：\n" + "\n".join(errs[:5])
    if route == "market/backtest":                               # 回测已并入行情页的「回测与验证」标签
        assert '<button class="on">回测与验证' in dom and "bt-run" in dom, "行情页没有打开「回测与验证」标签"
    if route == "market/backtest/score":
        assert "实盘成绩单" in dom and "sb-card" in dom
    if route == "settings":                                      # 默认打开「通用设置」：自动更新开关（A 股 / 美股各一个）在最上面
        assert "自动更新股票数据" in dom and dom.count("au-row") >= 2 and 'role="switch"' in dom, "设置页没有渲染出「自动更新」开关"
    if route == "settings/data":                                 # #/settings/data 直接打开「数据更新」：里面有数据健康面板
        assert "数据健康" in dom and ("hl-issue" in dom or "数据正常" in dom or "需要留意" in dom or "有问题" in dom), "设置 → 数据更新 没有渲染出「数据健康」面板"
    if route == "settings/upgrade":                              # 软件升级：打开就开始联网检查；没网也只显示错误说明，不能出 JS 报错
        assert "软件升级" in dom and ("正在联网检查更新" in dom or "已是最新版本" in dom or "发现" in dom or "检查失败" in dom or "alert" in dom), "设置 → 软件升级 没有渲染出来"
    if route == "settings/about":
        assert "免责声明" in dom, "设置 → 关于 没有渲染出版本信息"


def test_old_news_link_opens_the_news_tab_in_screener(base, tmp_path):
    """资讯已并入「选股」页：旧的 #/news 链接 / 书签要跳到选股页并打开「资讯」标签；导航栏里不再有单独的「资讯」。"""
    dom, errs = _visit(f"{base}/#/news", tmp_path / "p")
    assert 'data-view="screener"' in dom and not errs, errs[:3]
    assert '<button class="on" title="资讯' in dom, "没有自动打开「资讯」标签"
    assert 'data-nav="news"' not in dom


@pytest.mark.parametrize("old,new_has", [("backtest", "bt-run"), ("backtest/score", "sb-card")])
def test_old_backtest_link_opens_the_backtest_tab_in_market(base, tmp_path, old, new_has):
    """回测已并入「行情」页：旧的 #/backtest 链接 / 书签要跳到行情页的「回测与验证」标签；导航栏里不再有单独的「回测」。"""
    dom, errs = _visit(f"{base}/#/{old}", tmp_path / "p")
    assert 'data-view="market"' in dom and not errs, errs[:3]
    assert new_has in dom and 'data-nav="backtest"' not in dom


def test_without_login_the_page_stops_at_the_login_form(base_login, tmp_path):
    """开着登录、没有 Cookie：整个界面被登录页盖住（用户名 / 密码框 + 登录按钮），不会露出任何数据，也不会有 JS 报错。"""
    dom, errs = _visit(f"{base_login}/#/portfolio", tmp_path / "p")
    assert 'id="login-root"' in dom and 'name="username"' in dom and 'name="password"' in dom and "登录" in dom, "没有显示登录页"
    assert 'data-view="portfolio"' not in dom or "login-card" in dom
    assert "页面加载失败" not in dom
    assert not [e for e in errs if "401" not in e], errs[:3]                # 没登录时 /api/auth/me 返回 401 是预期的，浏览器会把它记成一行网络错误
