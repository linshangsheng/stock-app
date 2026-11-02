"""前端静态检查（环境里没有 node，没法跑 JS 单测，至少把最容易出错的「文件对不上」拦住）：
  - 每个 js/*.js 都进了离线缓存清单（sw.js），否则断网时该页面打不开
  - 每条相对 import 都能找到文件
  - sw.js 离线缓存清单里的每一项，服务器都真的能返回 200（cache.addAll 是整批原子的：有一项 404，整批都缓存失败）
  - 每个文件括号配对（字符串 / 注释 / 模板字符串内的括号不计）"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = sorted((ROOT / "js").glob("*.js"))


def test_every_js_file_is_in_service_worker_cache():
    sw = (ROOT / "sw.js").read_text(encoding="utf-8")
    missing = [f.name for f in JS if f"/js/{f.name}" not in sw]
    assert not missing, f"sw.js 的 SHELL_FILES 缺 {missing}"


def test_every_service_worker_shell_file_is_served():
    import re as _re
    import warnings
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    from fastapi.testclient import TestClient
    from server import main
    sw = (ROOT / "sw.js").read_text(encoding="utf-8")
    block = sw[sw.index("SHELL_FILES"):sw.index("];", sw.index("SHELL_FILES"))]
    paths = _re.findall(r"'(/[^']*)'", block)
    assert len(paths) > 20
    c = TestClient(main.app)
    bad = [p for p in paths if c.get(p).status_code != 200]
    assert not bad, f"sw.js 的 SHELL_FILES 里这些路径服务器返回的不是 200：{bad}"


def test_relative_imports_resolve():
    bad = []
    for f in JS:
        for m in re.finditer(r"""(?:from\s+|import\()\s*['"](\./[^'"]+)['"]""", f.read_text(encoding="utf-8")):
            if not (f.parent / m.group(1)).exists():
                bad.append(f"{f.name} -> {m.group(1)}")
    assert not bad, bad


def _strip(src: str) -> str:
    """去掉字符串、注释、正则字面量，只留代码里的括号。模板字符串里的 ${...} 当作代码保留。"""
    out, i, n = [], 0, len(src)
    stack = []                                           # 模板字符串嵌套：记录进入 ${ 时的花括号深度
    depth = 0
    while i < n:
        ch = src[i]
        nx = src[i + 1] if i + 1 < n else ""
        if ch == "/" and nx == "/":
            i = src.find("\n", i); i = n if i < 0 else i
        elif ch == "/" and nx == "*":
            i = src.find("*/", i + 2); i = n if i < 0 else i + 2
        elif ch in "'\"":
            j = i + 1
            while j < n and src[j] != ch:
                j += 2 if src[j] == "\\" else 1
            i = j + 1
        elif ch == "`":
            j = i + 1
            while j < n and src[j] != "`":
                if src[j] == "\\":
                    j += 2
                elif src[j] == "$" and j + 1 < n and src[j + 1] == "{":
                    k, d = j + 2, 1                      # 找到配对的 }，里面的内容递归检查
                    while k < n and d:
                        d += (src[k] == "{") - (src[k] == "}")
                        k += 1
                    out.append(_strip(src[j + 2:k - 1]))
                    j = k
                else:
                    j += 1
            i = j + 1
        elif ch == "/" and re.search(r"([(,=:\[!&|?{};]|\breturn)\s*$", "".join(out[-8:]) or "("):
            j = i + 1                                    # 正则字面量
            while j < n and src[j] not in "/\n":
                j += 2 if src[j] == "\\" else (src.find("]", j) - j + 1 if src[j] == "[" and src.find("]", j) > 0 else 1)
            i = j + 1
        else:
            out.append(ch); i += 1
            continue
    return "".join(out)


def test_brackets_balanced():
    pairs = {")": "(", "]": "[", "}": "{"}
    bad = []
    for f in JS:
        st = []
        for ch in _strip(f.read_text(encoding="utf-8")):
            if ch in "([{":
                st.append(ch)
            elif ch in pairs:
                if not st or st.pop() != pairs[ch]:
                    bad.append(f.name); break
        else:
            if st:
                bad.append(f.name)
    assert not bad, f"括号不配对：{bad}"
