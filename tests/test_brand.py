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
    from PIL import Image
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
    assert "api/ping" in html and "splash=1" in html
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
