"""生成使用手册（README.md）的界面截图：用本机 Edge 的无界面模式（DevTools 协议）打开应用、点按钮、截 PNG。
用法：先启动后端（例如 PORT=8011 python -m server.main），再运行
      python tools/screenshots.py --base http://127.0.0.1:8011 --out docs/images
只需要 Edge 与 Python 的 websockets 包（无需 Selenium / Playwright）；使用临时浏览器配置目录，不碰你自己的 Edge 配置。
截图只读页面，不点「保存 / 删除 / 成交」之类会改数据的按钮。"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


class Page:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def cmd(self, method, **params):
        self.n += 1
        my = self.n
        await self.ws.send(json.dumps({"id": my, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == my:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def js(self, expr: str, wait: float = 0.0):
        r = await self.cmd("Runtime.evaluate", expression=f"(async () => {{ {expr} }})()", awaitPromise=True, returnByValue=True)
        if wait:
            await asyncio.sleep(wait)
        return r.get("result", {}).get("value")

    async def goto(self, url: str, wait: float = 3.0):
        await self.cmd("Page.navigate", url=url)
        await asyncio.sleep(wait)

    async def shot(self, path: Path, clip: dict | None = None):
        params = {"format": "png", "captureBeyondViewport": False}
        if clip:
            params["clip"] = {**clip, "scale": 1}
        r = await self.cmd("Page.captureScreenshot", **params)
        path.write_bytes(base64.b64decode(r["data"]))
        print("  ", path.name, f"{path.stat().st_size // 1024} KB")

    async def tall_shot(self, path: Path, selector: str, width: int, height: int):
        """元素比窗口高时：临时把窗口调到够高（应用在自己的面板里滚动，窗口外的内容不会被渲染），截完恢复。"""
        r = await self.rect(selector)
        if r and r["height"] + 40 > height:
            await self.cmd("Emulation.setDeviceMetricsOverride", width=width, height=int(r["height"] + 80), deviceScaleFactor=1, mobile=False)
            await asyncio.sleep(1.5)
            r = await self.rect(selector)
        if r:
            await self.shot(path, r)
        await self.cmd("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)
        await asyncio.sleep(0.5)

    async def rect(self, selector: str, pad: int = 8):
        r = await self.js(f"const e = document.querySelector({json.dumps(selector)}); if (!e) return null; e.scrollIntoView({{block:'start'}}); "
                          f"await new Promise(r => setTimeout(r, 300)); const b = e.getBoundingClientRect(); return [b.x, b.y, b.width, b.height];")
        if not r:
            return None
        x, y, w, h = r
        return {"x": max(0, x - pad), "y": max(0, y - pad), "width": w + 2 * pad, "height": h + 2 * pad}


async def run(base: str, out: Path, width: int, height: int):
    import websockets

    out.mkdir(parents=True, exist_ok=True)
    prof = Path(tempfile.mkdtemp(prefix="edge-shot-"))
    import socket
    with socket.socket() as so:                     # 每次用一个空闲端口：绝不连到上一次没退出的浏览器
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    proc = subprocess.Popen([EDGE, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={prof}", "--no-first-run",
                             "--disable-extensions", f"--window-size={width},{height}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                page = next(t for t in targets if t.get("type") == "page")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
            p = Page(ws)
            await p.cmd("Page.enable")
            await p.cmd("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)
            await p.cmd("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "light"}])
            await p.goto(base + "/#/screener", 6)
            await p.js("localStorage.setItem('sa.theme', JSON.stringify('light')); localStorage.setItem('sa.autoFullscreen', 'false'); localStorage.setItem('sa.market', JSON.stringify('CN'));")
            await p.cmd("Page.reload", ignoreCache=True)        # 同一网址再 navigate 不会重新加载：必须 reload，设置才生效
            await asyncio.sleep(6)
            for f in out.glob("*.png"):
                f.unlink()                                     # 重新生成全部截图（编号可能变化）
            await p.shot(out / "01-选股首页.png")
            await p.shot(out / "02-市场温度与指数摘要.png", await p.rect(".mkt-brief"))
            await p.js("const tr = [...document.querySelectorAll('.mkt-brief tbody tr')].find(t => t.style.cursor === 'pointer'); tr && tr.click();", 0.5)
            await p.shot(out / "03-指数明天情景.png", await p.rect(".mkt-brief"))
            # 观察清单（列表列滚到标签栏）
            await p.goto(base + "/#/screener", 5)
            col = {"x": 92, "y": 0, "width": 392, "height": height}
            await p.js("document.querySelector('#col-list .tabs')?.scrollIntoView({block:'start'});", 0.5)
            await p.shot(out / "04-观察清单.png", col)
            # 个股：点开清单里的第一只 → 右侧详情「明天怎么操作」
            await p.js("document.querySelector('#col-list .list-item')?.click();", 5)
            await p.shot(out / "05-个股详情与操作.png")
            await p.shot(out / "06-明天怎么操作.png", await p.rect(".plan-card"))
            await p.js("[...document.querySelectorAll('#col-list .tabs button')].find(b => b.innerText === '次日执行')?.click();", 1)
            await p.js("document.querySelector('#col-list .tabs')?.scrollIntoView({block:'start'});", 0.5)
            await p.shot(out / "07-次日执行.png", col)
            # 成交了 → 记录买入表单：填一个高开后的成交价，表单提示最多该买几股（只看提示，点「取消」，不保存）
            await p.js("[...document.querySelectorAll('#col-list button')].find(b => b.innerText === '成交了')?.click();", 1)
            await p.js("const m = document.querySelector('#modal-root'); const ins = [...m.querySelectorAll('input[type=number]')]; "
                       "const set = (el, v) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles: true})); }; "
                       "if (ins[0]) set(ins[0], '10.79'); if (ins[1]) set(ins[1], '1500');", 0.5)
            await p.shot(out / "08-记录买入表单.png")
            await p.js("[...document.querySelectorAll('#modal-root button')].find(b => b.innerText.trim() === '取消')?.click();", 0.5)
            await p.js("[...document.querySelectorAll('#col-list .tabs button')].find(b => b.innerText === '历史回看')?.click();", 2)
            await p.js("document.querySelector('#col-list .tabs')?.scrollIntoView({block:'start'});", 0.5)
            await p.shot(out / "09-历史回看.png", col)
            # 低风险组合：左侧名单与操作、右侧回测证据（证据在后台算，最多等 3 分钟）。先重新加载，关掉上面点开的股票详情
            await p.cmd("Page.reload", ignoreCache=True)
            await asyncio.sleep(6)
            await p.js("[...document.querySelectorAll('#col-list .tabs button')].find(b => b.innerText === '低风险组合')?.click();", 3)
            for _ in range(36):
                if await p.js("return !!document.querySelector('#col-detail .fp-ev');"):
                    break
                await asyncio.sleep(5)
            await asyncio.sleep(2)
            await p.js("document.querySelector('#col-list .tabs')?.scrollIntoView({block:'start'});", 0.5)
            await p.shot(out / "21-低风险组合.png")
            await p.tall_shot(out / "22-低风险组合-回测证据.png", "#col-detail .fp-ev", width, height)
            await p.js("document.querySelector('#col-list .fp-list')?.scrollIntoView({block:'start'});", 0.5)
            await p.shot(out / "23-低风险组合-目标名单.png", col)
            # 行情页
            await p.goto(base + "/#/market", 8)
            await p.shot(out / "10-行情-市场温度.png")
            await p.tall_shot(out / "11-行情-宽基指数规则.png", "#col-page .card:nth-of-type(2)", width, height)
            await p.js("const d = document.querySelectorAll('#col-page details.card')[1]; if (d) { d.open = true; d.dispatchEvent(new Event('toggle')); }", 2)
            await p.tall_shot(out / "12-行情-指数详情与回测.png", "#col-page details.card[open]", width, height)
            # 持仓
            await p.goto(base + "/#/portfolio", 4)
            await p.shot(out / "13-持仓页.png")
            # 回测 / 设置
            await p.goto(base + "/#/backtest", 4)
            await p.shot(out / "14-回测页.png")
            await p.goto(base + "/#/settings", 12)
            await p.shot(out / "15-设置-数据与更新.png")
            st = await p.js("const c = [...document.querySelectorAll('#col-page .card')].find(x => x.innerText.startsWith('资金方案')); "
                            "if (!c) return null; c.id = 'shot-alloc'; return true;")
            if st:
                await p.shot(out / "24-设置-资金方案.png", await p.rect("#shot-alloc"))
            st = await p.js("const c = [...document.querySelectorAll('#col-page .card')].find(x => x.innerText.startsWith('费用、滑点')); "
                            "if (!c) return null; c.id = 'shot-cost'; return true;")
            if st:
                await p.shot(out / "16-设置-费用与组合参数.png", await p.rect("#shot-cost"))
            # 深色模式与手机（窄屏）
            await p.goto(base + "/#/screener", 2)
            await p.js("localStorage.setItem('sa.theme', JSON.stringify('dark'));")
            await p.cmd("Page.reload", ignoreCache=True)
            await asyncio.sleep(6)
            await p.js("document.querySelector('#col-list .list-item')?.click();", 5)
            await p.shot(out / "18-深色模式.png")
            await p.js("localStorage.setItem('sa.theme', JSON.stringify('light'));")
            await p.cmd("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=2, mobile=True)
            await p.cmd("Page.reload", ignoreCache=True)
            await asyncio.sleep(6)
            await p.shot(out / "19-手机-首页.png")
            await p.js("document.querySelector('#col-list .list-item')?.click();", 5)
            r = await p.rect(".plan-card", pad=4)
            await p.shot(out / "20-手机-明天怎么操作.png", {"x": 0, "y": r["y"], "width": 390, "height": min(r["height"], 1400)} if r else None)
            await p.cmd("Page.reload", ignoreCache=True)
            await asyncio.sleep(6)
            await p.js("[...document.querySelectorAll('#col-list .tabs button')].find(b => b.innerText === '低风险组合')?.click();", 4)
            await p.js("document.querySelector('.fp-tab')?.scrollIntoView({block:'start'});", 1)
            await p.shot(out / "25-手机-低风险组合.png")
            await p.cmd("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)
            await p.goto(base + "/#/settings", 8)
            st = await p.js("const c = [...document.querySelectorAll('#col-page .card')].find(x => x.innerText.startsWith('存储空间')); "
                            "if (!c) return null; c.id = 'shot-storage'; return true;")
            if st:
                await p.shot(out / "17-设置-存储空间.png", await p.rect("#shot-storage"))
    finally:
        # Edge 启动器会把真正的浏览器进程分离出去：先通过 DevTools 让浏览器自己关闭，
        # 再按「本次的临时配置目录」兜底结束残留进程（只会命中本脚本启动的那个无界面 Edge）。
        try:
            ver = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3))
            async with websockets.connect(ver["webSocketDebuggerUrl"]) as bws:
                await bws.send(json.dumps({"id": 1, "method": "Browser.close"}))
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.5)
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Get-CimInstance Win32_Process -Filter \"Name = 'msedge.exe'\" | Where-Object {{ $_.CommandLine -like '*{prof.name}*' }} | "
                        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(20):
            shutil.rmtree(prof, ignore_errors=True)
            if not prof.exists():
                break
            time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8011")
    ap.add_argument("--out", default="docs/images")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    a = ap.parse_args()
    asyncio.run(run(a.base.rstrip("/"), Path(a.out), a.width, a.height))


if __name__ == "__main__":
    main()
