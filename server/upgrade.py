"""软件升级（设置 → 软件升级）：从 git 仓库把新代码更新到本机，再自动重启后端（页面会自己刷新）。

怎么更新（保守、只前进、不丢东西）：
  1. 检查环境：找得到 git、这个目录是 git 仓库、有 origin 远程、不是游离状态。
  2. 下载更新：git fetch origin（真实进度来自 git --progress 输出）。
  3. 判断：远程没有新提交 → 已是最新；本机有没推送的提交且远程也有新的（分叉）→ 不处理，让用户自己决定；
     本机改过、而且更新也要改的文件 → 不处理（不会覆盖你的改动）。
  4. 依赖：requirements.txt 变了就先装新依赖（装失败时代码一个字都还没动）。
  5. 应用：git merge --ff-only（只允许「快进」，不会产生合并提交，也不会丢本机已提交的内容）。
  6. 重启后端：沿用 server/restart.py 的接班机制；前端轮询到新进程后刷新页面。
不用 reset --hard / clean / stash：任何一步出问题都不会删东西。data/（行情库、个人库、备份）不在版本库里，升级不碰它。"""
from __future__ import annotations

import copy
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import restart, settings

ROOT = Path(__file__).resolve().parent.parent
REQ = "server/requirements.txt"

# 步骤与各步在进度环上占的区间（下载是最慢的，占一半；依赖没变就直接跳过）
STEPS = [("check", "检查环境", 0, 8), ("fetch", "下载更新", 8, 55), ("deps", "更新依赖库", 55, 80), ("merge", "应用新代码", 80, 90), ("restart", "重启软件", 90, 100)]

_lock = threading.Lock()
_state: dict = {}


class UpgradeError(Exception):
    """可以直接念给用户听的错误。"""


# ---------------------------------------------------------------- git
def find_git() -> str | None:
    import shutil
    g = shutil.which("git")
    if g:
        return g
    for p in (r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files (x86)\Git\cmd\git.exe", str(Path.home() / "AppData/Local/Programs/Git/cmd/git.exe")):
        if os.path.exists(p):
            return p
    return None


def _env() -> dict:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "LC_ALL": "C", "LANGUAGE": "C"}      # 强制 git 输出英文：进度行、错误信息才好识别


def _flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0              # 没有控制台的后端进程里调用 git，不要闪出黑窗口


def run(cmd: list[str], root: Path, timeout: float = 60, on_line=None) -> tuple[int, str]:
    """运行命令，返回 (退出码, 合并后的输出)。on_line(行) 逐行回调（git / pip 的进度行用 \\r 或 \\n 分隔）。超时会结束进程。"""
    try:
        p = subprocess.Popen(cmd, cwd=str(root), env=_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=_flags())
    except OSError as e:
        return 127, str(e)
    timed_out = []
    timer = threading.Timer(timeout, lambda: (timed_out.append(1), p.kill()))
    timer.start()
    buf, out = b"", []
    try:
        while True:
            chunk = p.stdout.read1(2048) if hasattr(p.stdout, "read1") else p.stdout.read(2048)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for part in parts:
                line = part.decode("utf-8", "replace").strip()
                if line:
                    out.append(line)
                    if on_line:
                        on_line(line)
        if buf.strip():
            line = buf.decode("utf-8", "replace").strip()
            out.append(line)
            if on_line:
                on_line(line)
        p.wait()
    finally:
        timer.cancel()
    if timed_out:
        out.append("（超时，已中止）")
    return p.returncode, "\n".join(out)


def _git(git: str, args: list[str], root: Path, **kw) -> tuple[int, str]:
    return run([git, "-c", "core.quotepath=false", *args], root, **kw)


def _friendly_git_error(text: str) -> str:
    t = text.lower()
    if "host key verification failed" in t:
        return ("连不上远程仓库：这台电脑还没信任 GitHub 的主机密钥（第一次用 SSH 连 GitHub 都要先确认一次，而这里没法弹出确认）。"
                "在服务器上用运行软件的那个用户执行一次：ssh-keyscan github.com >> ~/.ssh/known_hosts ，再回来升级。\n原始信息：" + text[-300:])
    if "permission denied" in t or "publickey" in t or "authentication failed" in t or "could not read username" in t:
        return "连不上远程仓库：没有权限（这台电脑没有配置好 GitHub 的登录 / SSH 密钥）。先在命令行里 git fetch 一次确认能连上，再回来升级。\n原始信息：" + text[-300:]
    if "could not resolve host" in t or "connection timed out" in t or "unable to access" in t or "network is unreachable" in t or "超时" in text:
        return "连不上远程仓库：网络不通或超时（可能需要代理 / 梯子）。网络好了再试。\n原始信息：" + text[-300:]
    return text[-400:] or "git 没有给出错误信息"


# ---------------------------------------------------------------- 检查
def _preflight(root: Path) -> tuple[str, str]:
    """返回 (git 路径, 当前分支)；有问题抛 UpgradeError。"""
    git = find_git()
    if not git:
        raise UpgradeError("找不到 git：这台电脑没有安装 Git，或者软件找不到它。请先安装 Git（Windows：git-scm.com 下载 Git for Windows；Ubuntu：sudo apt install git），再试。")
    if not (root / ".git").exists():
        raise UpgradeError("这份软件不是用 git 下载的（目录里没有 .git），没法自动升级。请用 git clone 重新下载一份，或手动覆盖新文件。")
    code, out = _git(git, ["rev-parse", "--abbrev-ref", "HEAD"], root)
    branch = out.strip()
    if code != 0 or not branch:
        raise UpgradeError("读不出当前分支：" + out[-200:])
    if branch == "HEAD":
        raise UpgradeError("现在处于「游离 HEAD」状态（不在任何分支上），自动升级不处理这种情况。请先 git checkout main。")
    code, out = _git(git, ["remote", "get-url", "origin"], root)
    if code != 0:
        raise UpgradeError("没有配置远程仓库（origin），不知道从哪里升级。")
    return git, branch


def _commit_info(git: str, root: Path, rev: str) -> dict | None:
    code, out = _git(git, ["log", "-1", "--format=%h%x1f%s%x1f%cs", rev], root)
    if code != 0 or not out.strip():
        return None
    h, s, d = (out.strip().split("\x1f") + ["", "", ""])[:3]
    return {"hash": h, "subject": s, "date": d}


def _inspect(git: str, root: Path, branch: str) -> dict:
    """不联网：比较本地和 origin/<分支>（要先 fetch 过）。"""
    ref = f"origin/{branch}"
    code, _ = _git(git, ["rev-parse", "--verify", "--quiet", ref], root)
    if code != 0:
        raise UpgradeError(f"远程仓库里没有分支 {branch}（{ref}）。")
    code, out = _git(git, ["rev-list", "--left-right", "--count", f"HEAD...{ref}"], root)
    ahead, behind = (int(x) for x in out.split()[:2]) if code == 0 and len(out.split()) >= 2 else (0, 0)
    commits = []
    if behind:
        _, out = _git(git, ["log", f"HEAD..{ref}", "--format=%h%x1f%s%x1f%cs", "-n", "30"], root)
        for line in out.splitlines():
            p = line.split("\x1f")
            if len(p) == 3:
                commits.append({"hash": p[0], "subject": p[1], "date": p[2]})
    _, out = _git(git, ["diff", "--name-only", "HEAD", ref], root)
    changed = [x for x in out.splitlines() if x.strip()]
    _, out = _git(git, ["diff", "--name-only", "HEAD"], root)                      # 本机对已跟踪文件的改动（已暂存 + 未暂存）；未跟踪的新文件不会和更新冲突
    dirty = [x.strip('"') for x in out.splitlines() if x.strip()]
    clash = sorted(set(dirty) & set(changed)) if behind else []
    reason, can = None, False
    if behind == 0:
        reason = None
    elif ahead:
        reason = f"本机有 {ahead} 个还没推送到远程的提交，而远程也有 {behind} 个新提交——两边分叉了。自动升级不处理这种情况（怕丢你的提交），请先在开发环境里合并 / 推送。"
    elif clash:
        reason = "这些文件你在本机改过、还没提交，而更新也要改它们，直接升级会冲突：" + "、".join(clash[:8]) + ("…" if len(clash) > 8 else "") + "。请先提交或还原这些改动。"
    else:
        can = True
    return {"branch": branch, "ahead": ahead, "behind": behind, "commits": commits, "changed_files": len(changed), "requirements_changed": REQ in changed,
            "dirty_files": dirty[:20], "clash": clash, "can_upgrade": can, "reason": reason, "up_to_date": behind == 0}


def check(root: Path | None = None) -> dict:
    """「检查更新」：联网 fetch 一次，返回当前版本和有哪些新提交。不改任何文件（只更新 .git 里的远程跟踪记录）。"""
    root = Path(root or ROOT)
    try:
        git, branch = _preflight(root)
        code, out = _git(git, ["fetch", "origin", branch], root, timeout=90)
        if code != 0:
            raise UpgradeError(_friendly_git_error(out))
        info = _inspect(git, root, branch)
    except UpgradeError as e:
        return {"ok": False, "error": str(e), "checked_at": datetime.now().isoformat(timespec="seconds")}
    return {"ok": True, "current": _commit_info(git, root, "HEAD"), "checked_at": datetime.now().isoformat(timespec="seconds"), **info}


# ---------------------------------------------------------------- 升级（后台线程 + 进度）
def _fresh() -> dict:
    return {"running": False, "percent": 0, "step": None, "message": "", "error": None, "done": False, "restarting": False, "up_to_date": False,
            "steps": [{"key": k, "label": l, "state": "todo"} for k, l, _, _ in STEPS], "log": [], "pid": os.getpid(), "started_at": None, "old": None, "new": None}


_state.update(_fresh())


def status() -> dict:
    with _lock:
        return copy.deepcopy(_state)


def _set(step: str | None = None, pct: float | None = None, msg: str | None = None, state: str | None = None, **kw) -> None:
    with _lock:
        if step:
            _state["step"] = step
            if state:
                for s in _state["steps"]:
                    if s["key"] == step:
                        s["state"] = state
        if pct is not None:
            _state["percent"] = max(_state["percent"], min(100, int(pct)))      # 进度只增不减
        if msg is not None:
            _state["message"] = msg
        _state.update(kw)


def _log(line: str) -> None:
    with _lock:
        _state["log"].append(line)
        del _state["log"][:-60]


def _history(text: str) -> None:
    try:
        with open(settings.data_dir() / "upgrade.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {text}\n")
    except OSError:
        pass


def _span(key: str) -> tuple[int, int]:
    for k, _, a, b in STEPS:
        if k == key:
            return a, b
    return 0, 100


def start(root: Path | None = None, restart_fn=None) -> dict:
    """开始升级（后台线程）。已经在升级就不重复开。restart_fn 默认是 restart.schedule（测试里可替换）。"""
    with _lock:
        if _state.get("running"):
            return {"ok": False, "already": True}
        _state.clear()
        _state.update(_fresh())
        _state.update(running=True, started_at=datetime.now().isoformat(timespec="seconds"))
    threading.Thread(target=_run, args=(Path(root or ROOT), restart_fn or restart.schedule), daemon=True, name="upgrade").start()
    return {"ok": True}


def _skip(step: str) -> None:
    _set(step, _span(step)[1], state="skip")


def _run(root: Path, restart_fn) -> None:
    try:
        _upgrade(root, restart_fn)
    except UpgradeError as e:
        _fail(str(e))
    except Exception as e:  # noqa: BLE001
        _fail(f"升级过程出了意外：{type(e).__name__}: {e}")


def _fail(msg: str) -> None:
    with _lock:
        for s in _state["steps"]:
            if s["state"] == "doing":
                s["state"] = "error"
        _state.update(running=False, error=msg, message="升级没有完成")
    _log("× " + msg.splitlines()[0])
    _history("失败：" + msg.splitlines()[0])


def _upgrade(root: Path, restart_fn) -> None:
    # 1 检查环境
    _set("check", 1, "正在检查环境…", state="doing")
    git, branch = _preflight(root)
    old = _commit_info(git, root, "HEAD")
    _set(pct=_span("check")[1], old=old)
    _set("check", state="done")
    _log(f"当前版本 {old['hash'] if old else '?'}（分支 {branch}）")

    # 2 下载（真实进度：git --progress 的「Receiving objects: 45%」）
    a, b = _span("fetch")
    _set("fetch", a, "正在从远程仓库下载更新…", state="doing")
    last = {"p": 0}

    def on_line(line: str) -> None:
        m = re.search(r"(Receiving objects|Resolving deltas|Counting objects|Compressing objects):\s+(\d+)%", line)
        if m:
            weight = {"Counting objects": (0, .1), "Compressing objects": (.1, .2), "Receiving objects": (.2, .85), "Resolving deltas": (.85, 1)}[m.group(1)]
            frac = weight[0] + (weight[1] - weight[0]) * int(m.group(2)) / 100
            _set(pct=a + (b - a) * frac, msg=f"正在下载更新… {m.group(1).replace('Receiving objects', '接收').replace('Resolving deltas', '整理').replace('Counting objects', '统计').replace('Compressing objects', '压缩')} {m.group(2)}%")
        elif line != last.get("l"):
            last["l"] = line
            _log(line[:200])

    code, out = _git(git, ["fetch", "--progress", "origin", branch], root, timeout=300, on_line=on_line)
    if code != 0:
        raise UpgradeError(_friendly_git_error(out))
    info = _inspect(git, root, branch)
    _set("fetch", b, state="done")
    if info["up_to_date"]:
        for k in ("deps", "merge", "restart"):
            _skip(k)
        with _lock:
            _state.update(running=False, done=True, up_to_date=True, percent=100, message="已经是最新版本，不需要升级。")
        _log("已是最新")
        return
    if not info["can_upgrade"]:
        raise UpgradeError(info["reason"] or "现在不能自动升级。")
    _log(f"有 {info['behind']} 个新提交，涉及 {info['changed_files']} 个文件")

    # 3 依赖：先装，装失败时代码还没动
    ref = f"origin/{branch}"
    a, b = _span("deps")
    if info["requirements_changed"]:
        _set("deps", a, "正在更新依赖库（可能要一两分钟）…", state="doing")
        code, req = _git(git, ["show", f"{ref}:{REQ}"], root)
        if code != 0:
            raise UpgradeError("读不到新版的依赖清单：" + req[-200:])
        tmp = settings.data_dir() / "upgrade_requirements.txt"
        tmp.write_text(req + "\n", encoding="utf-8")
        n = {"i": 0}

        def pip_line(line: str) -> None:
            n["i"] += 1
            _set(pct=a + (b - a) * (1 - 0.92 ** n["i"]), msg="正在更新依赖库… " + line[:60])
            if re.match(r"(Collecting|Downloading|Installing|Successfully|ERROR)", line):
                _log(line[:200])

        code, out = run([sys.executable, "-m", "pip", "install", "-r", str(tmp), "--disable-pip-version-check", "--no-input"], root, timeout=900, on_line=pip_line)
        if code != 0:
            raise UpgradeError("依赖库安装失败（代码还没有改动，软件照常能用）：\n" + out[-400:])
        _set("deps", b, state="done")
    else:
        _skip("deps")

    # 4 应用：只快进
    a, b = _span("merge")
    _set("merge", a, "正在应用新代码…", state="doing")
    code, out = _git(git, ["merge", "--ff-only", ref], root, timeout=120)
    if code != 0:
        raise UpgradeError("应用新代码失败（没有改动任何文件）：" + out[-300:])
    new = _commit_info(git, root, "HEAD")
    _set("merge", b, state="done", new=new)
    _log(f"已更新到 {new['hash'] if new else '?'}")
    _history(f"{old['hash'] if old else '?'} → {new['hash'] if new else '?'}（{info['behind']} 个提交）")

    # 5 重启
    a, _ = _span("restart")
    _set("restart", a + 2, "代码已更新，正在重启后端…", state="doing", restarting=True)
    time.sleep(0.4)
    restart_fn()
    with _lock:
        _state.update(done=True, running=False)
    _log("已请求重启后端")
