"""把 icon.svg / favicon.svg 渲染成桌面快捷方式用的 app.ico 和 PWA / 手机用的 PNG（用本机 Edge 无界面模式渲染，透明背景）。
用法：python tools/make_icons.py
产物：app.ico（16~256 多尺寸，桌面「Stock App」快捷方式用）、icons/icon-192.png、icons/icon-512.png、icons/apple-touch-icon.png（180）、
      icons/maskable-512.png（安卓自适应图标：四周留安全边）、icons/favicon-32.png
小尺寸（16 / 24 / 32）用简化版 favicon.svg：细节少、笔画粗，任务栏和文件夹里看得清。"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from screenshots import EDGE, Page  # noqa: E402

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;padding:0;background:transparent;overflow:hidden}}
.box{{width:{n}px;height:{n}px;{pad}}} svg{{width:100%;height:100%;display:block}}
</style></head><body><div class="box">{svg}</div></body></html>"""


async def render(page: Page, svg: str, n: int, tmp: Path, pad: float = 0.0, bg: str | None = None) -> bytes:
    style = f"box-sizing:border-box;padding:{int(n * pad)}px;" + (f"background:{bg};" if bg else "")
    f = tmp / f"r{n}_{int(pad * 100)}.html"
    f.write_text(PAGE.format(n=n, svg=svg, pad=style), encoding="utf-8")
    await page.cmd("Emulation.setDeviceMetricsOverride", width=n, height=n, deviceScaleFactor=1, mobile=False)
    await page.cmd("Emulation.setDefaultBackgroundColorOverride", color={"r": 0, "g": 0, "b": 0, "a": 0})
    await page.goto(f.as_uri(), 0.6)
    r = await page.cmd("Page.captureScreenshot", format="png", clip={"x": 0, "y": 0, "width": n, "height": n, "scale": 1})
    return base64.b64decode(r["data"])


async def run():
    import websockets
    from PIL import Image

    big = (ROOT / "icon.svg").read_text(encoding="utf-8")
    small = (ROOT / "favicon.svg").read_text(encoding="utf-8")
    tmp = Path(tempfile.mkdtemp(prefix="icons-"))
    prof = Path(tempfile.mkdtemp(prefix="edge-icon-"))
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    proc = subprocess.Popen([EDGE, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={prof}", "--no-first-run",
                             "--disable-extensions", "--allow-file-access-from-files", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out_dir = ROOT / "icons"
    out_dir.mkdir(exist_ok=True)
    try:
        for _ in range(50):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                target = next(t for t in targets if t.get("type") == "page")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
            p = Page(ws)
            await p.cmd("Page.enable")
            frames = {}
            for n in (16, 24, 32):
                frames[n] = await render(p, small, n, tmp)
            for n in (48, 64, 128, 256):
                frames[n] = await render(p, big, n, tmp)
            imgs = {n: Image.open(io.BytesIO(b)).convert("RGBA") for n, b in frames.items()}
            imgs[256].save(ROOT / "app.ico", format="ICO", sizes=[(n, n) for n in sorted(imgs)],
                           append_images=[imgs[n] for n in sorted(imgs) if n != 256])
            for name, svg, n, pad, bg in (("icon-192.png", big, 192, 0, None), ("icon-512.png", big, 512, 0, None),
                                          ("apple-touch-icon.png", big, 180, 0, "#0b1230"), ("favicon-32.png", small, 32, 0, None),
                                          ("maskable-512.png", big, 512, 0.1, "#0b1230")):
                (out_dir / name).write_bytes(await render(p, svg, n, tmp, pad, bg))
            print("生成：app.ico（" + " / ".join(str(n) for n in sorted(imgs)) + "）、icons/*.png")
    finally:
        try:
            ver = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3))
            async with websockets.connect(ver["webSocketDebuggerUrl"]) as bws:
                await bws.send(json.dumps({"id": 1, "method": "Browser.close"}))
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0)
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Get-CimInstance Win32_Process -Filter \"Name = 'msedge.exe'\" | Where-Object {{ $_.CommandLine -like '*{prof.name}*' }} | "
                        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for d in (prof, tmp):
            for _ in range(20):
                shutil.rmtree(d, ignore_errors=True)
                if not d.exists():
                    break
                time.sleep(0.3)


if __name__ == "__main__":
    asyncio.run(run())
