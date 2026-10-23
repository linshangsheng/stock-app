"""图标与启动画面：静态文件都在、后端能提供、启动脚本保持纯 ASCII（VBScript 读不了 UTF-8 中文）、启动画面探测的是 /api/ping。"""
import json
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)
ROOT = Path(__file__).resolve().parent.parent


def test_manifest_icons_exist_and_ico_has_all_sizes():
    m = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    for ic in m["icons"]:
        assert (ROOT / ic["src"]).exists(), ic["src"]
    assert any(ic.get("purpose") == "maskable" for ic in m["icons"])
    import pytest
    Image = pytest.importorskip("PIL.Image", reason="Pillow 只是生成图标的工具依赖，软件本身不需要")
    sizes = Image.open(ROOT / "app.ico").info["sizes"]
    assert {(n, n) for n in (16, 24, 32, 48, 64, 128, 256)} <= set(sizes)


def test_launcher_is_ascii_and_opens_splash():
    b = (ROOT / "启动股票.vbs").read_bytes()
    assert all(x < 128 for x in b)
    s = b.decode("ascii")
    assert "splash.html?url=" in s and "app.ico" in s and "python -m server.main" in s


def test_splash_and_icons_served(demo_env):
    from fastapi.testclient import TestClient
    from server import main
    c = TestClient(main.app)
    for path, ctype in (("/splash.html", "text/html"), ("/favicon.svg", "image/svg+xml"), ("/icon.svg", "image/svg+xml"),
                        ("/icons/icon-192.png", "image/png")):
        r = c.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith(ctype), path
    html = c.get("/splash.html").text
    assert "favicon.svg?t=" in html and "splash=1" in html and "qs.get('alt')" in html   # 用 favicon 当指纹探测主端口和备用端口
    assert 'id="boot"' in c.get("/").text


def test_about_single_source(demo_env):
    from fastapi.testclient import TestClient
    import server
    from server import main
    c = TestClient(main.app)
    a = c.get("/api/about").json()
    assert a["author"] == server.AUTHOR == "林上升" and a["email"] == server.EMAIL == "linshangsheng1987@gmail.com"
    assert a["version"] == server.__version__ == main.app.version and a["copyright"].startswith("©")
    assert a["data_sources"] and any("Lightweight Charts" in x["name"] for x in a["third_party"])
    assert c.get("/api/ping").json()["version"] == server.__version__
    assert a["license"] == server.LICENSE == "MIT"
    lic = c.get("/LICENSE")
    assert lic.status_code == 200 and "MIT License" in lic.text and server.EMAIL in lic.text and "林上升" in lic.text
    from server import datasource_events
    assert server.EMAIL in datasource_events.SEC_UA
    assert f"v{server.__version__}" in (Path(__file__).resolve().parent.parent / "sw.js").read_text(encoding="utf-8")


def test_installer_bat_is_gbk_crlf_and_covers_the_steps():
    b = (ROOT / "安装.bat").read_bytes()
    s = b.decode("gbk")                                    # 中文 Windows 命令行按 GBK 显示
    assert b"\r\n" in b and b.count(b"\n") == b.count(b"\r\n")
    assert "chcp 936" in s and "server\\requirements.txt" in s and "pypi.tuna.tsinghua.edu.cn" in s
    assert "Add python.exe to PATH" in s and "启动股票.vbs" in s and "/quiet" in s



def test_pick_port_falls_back_when_preferred_is_unavailable():
    """8000 被 Windows 保留或被占用时换备用端口（这里用一个正在监听的端口模拟「用不了」）。"""
    import socket
    from server import main
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    bp = busy.getsockname()[1]
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    fp = free.getsockname()[1]
    free.close()
    try:
        assert main.pick_port("127.0.0.1", bp, [fp]) == fp
        assert main.pick_port("127.0.0.1", fp, [bp]) == fp
    finally:
        busy.close()


def test_launcher_ports_match_config():
    import re
    from server import settings
    s = settings.load_config(reload=True)["server"]
    vbs = (ROOT / "启动股票.vbs").read_text(encoding="ascii")
    ports = [int(x) for x in re.search(r"ports = Array\(([^)]*)\)", vbs).group(1).split(",")]
    alt = [int(x) for x in re.search(r'altPorts = "([^"]*)"', vbs).group(1).split(",")]
    assert ports == [s["port"], *s["fallback_ports"]] and alt == list(s["fallback_ports"])
    assert "&alt=" in vbs and "IsOurs" in vbs


def test_requirements_ascii_and_deps_check_matches(monkeypatch, capsys):
    """安装清单必须是纯 ASCII：旧版 pip 在中文 Windows 上按 GBK 读，遇到 UTF-8 中文直接报 UnicodeDecodeError（2026-10-23 实测）。
    启动前的依赖检查：齐全时不拦；缺了给出中文说明并以退出码 3 结束（双击启动 / start.bat 据此转去「安装.bat」）。"""
    import importlib.util
    import pytest
    from server import deps
    req = (ROOT / "server" / "requirements.txt").read_bytes()
    assert all(b < 128 for b in req)
    names = {line.split("==")[0].split("#")[0].strip().lower() for line in req.decode().splitlines() if line.strip() and not line.startswith("#")}
    assert {pkg.lower() for _, pkg in deps.REQUIRED} <= names
    assert deps.missing() == []
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda m, *a: None if m == "yfinance" else real(m, *a))
    with pytest.raises(SystemExit) as e:
        deps.require()
    assert e.value.code == 3 and "yfinance" in capsys.readouterr().out


def test_first_run_paths_send_users_to_the_installer():
    """新电脑没装依赖：start.bat 与双击启动都先检查（python -m server.deps），缺了就运行「安装.bat」，而不是让后端悄悄退出。
    安装.bat 只接受 Python 3.12~3.14（numpy 2.5 需要 3.12+；3.15 还没有 numpy / pandas / PyYAML 的安装包）。"""
    vbs = (ROOT / "启动股票.vbs").read_text(encoding="ascii")
    assert "python -m server.deps" in vbs and "ChrW(&H5B89) & ChrW(&H88C5)" in vbs          # 「安装」两个字用 ChrW 拼：VBS 源文件必须是 ASCII
    assert vbs.index("python -m server.deps") < vbs.index('sh.Run "cmd /c python -m server.main')   # 先检查、再启动后端
    b = (ROOT / "start.bat").read_bytes()
    s = b.decode("gbk")
    assert b.count(b"\n") == b.count(b"\r\n") and "chcp 936" in s and "server.deps" in s and '安装.bat" /nolaunch' in s
    inst = (ROOT / "安装.bat").read_bytes().decode("gbk")
    assert "(3, 12)" in inst and "(3, 15)" in inst and ":newpython" in inst and "/nolaunch" in inst
