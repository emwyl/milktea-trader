#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""发版前一致性检查 / 版本号统一管理（v20260911150 引入）。

## 为什么需要这个脚本

本项目发布包 `publish/` **同时包含两份代码副本**：
    publish/frontend/index.html   ← 对应 frontend/index.html
    publish/backend/app/**        ← 对应 backend/app/**（publish/start.sh 真正启动的就是它）

而 `publish/**` 在 .gitignore 里是**整体忽略**的（因为含明文会话密钥与用户数据库），
只有 `publish/backend/seed_quotes.py` 被例外放行。这意味着两份副本**完全靠手工同步、
没有任何机制保障**，一旦漏同步就会出现「前端已更新、后端还是旧代码」的静默故障：
新接口 404、新字段缺失，而这些失败大多被前端「静默降级」吞掉，页面上几乎看不出来 ——
实测已发生过（market-regime / 复盘报告 这一批：前端已同步，publish/backend 仍是旧版）。

同时，历史上版本号硬编码在前端 HTML 里，只能证明「前端是哪一版」，无法反映后端版本。

## 用法

    python scripts/release_check.py            # 检查（默认）—— 有任何问题非零退出
    python scripts/release_check.py --bump     # 把版本号升到「今天 + 提交数+1」，并同步改前端常量
    python scripts/release_check.py --sync     # 把 frontend/ 与 backend/app/ 同步到 publish/
    python scripts/release_check.py --all      # 先 --sync 再 --check（发版标准流程）

建议发版流程：`--bump` → 提交 → `--all`。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_PY = ROOT / "backend" / "app" / "version.py"
FRONTEND = ROOT / "frontend" / "index.html"
PUB_FRONTEND = ROOT / "publish" / "frontend" / "index.html"
BACKEND_APP = ROOT / "backend" / "app"
PUB_BACKEND_APP = ROOT / "publish" / "backend" / "app"

_OK, _BAD, _WARN = "[ OK ]", "[FAIL]", "[WARN]"
_problems: list[str] = []
_warnings: list[str] = []


def fail(msg: str) -> None:
    _problems.append(msg)
    print(f"{_BAD} {msg}")


def warn(msg: str) -> None:
    _warnings.append(msg)
    print(f"{_WARN} {msg}")


def ok(msg: str) -> None:
    print(f"{_OK} {msg}")


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def read_backend_version() -> str | None:
    if not VERSION_PY.exists():
        return None
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', VERSION_PY.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def read_frontend_version() -> str | None:
    if not FRONTEND.exists():
        return None
    m = re.search(r"var\s+FRONTEND_VERSION\s*=\s*'([^']+)'", FRONTEND.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def git_commit_count() -> int | None:
    try:
        out = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=20)
        return int(out.stdout.strip()) if out.returncode == 0 else None
    except Exception:
        return None


def expected_version(commit_no: int, day: str | None = None) -> str:
    day = day or dt.datetime.now().strftime("%Y%m%d")
    return f"v{day}{commit_no:03d}"


# ============ 版本号 ============

def bump() -> int:
    n = git_commit_count()
    if n is None:
        fail("无法读取 git 提交数（--bump 需要 git 仓库）")
        return 1
    new = expected_version(n + 1)
    old = read_backend_version()

    txt = VERSION_PY.read_text(encoding="utf-8")
    txt = re.sub(r'^APP_VERSION\s*=\s*"[^"]+"', f'APP_VERSION = "{new}"', txt, count=1, flags=re.M)
    VERSION_PY.write_text(txt, encoding="utf-8")

    fe = FRONTEND.read_text(encoding="utf-8")
    fe, k = re.subn(r"var\s+FRONTEND_VERSION\s*=\s*'[^']+'",
                    f"var FRONTEND_VERSION = '{new}'", fe, count=1)
    if k != 1:
        fail("前端未找到 FRONTEND_VERSION 常量，无法同步版本号")
        return 1
    FRONTEND.write_text(fe, encoding="utf-8")

    ok(f"版本号 {old} → {new}（提交数 {n} + 1）")
    print("     已同步：backend/app/version.py、frontend/index.html")
    print("     下一步：git commit → python scripts/release_check.py --all")
    return 0


# ============ 同步副本 ============

def _sync_tree(src: Path, dst: Path, label: str) -> int:
    """把 src 目录同步到 dst（只增改，不删除；跳过 __pycache__）。"""
    if not dst.exists():
        dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in src.rglob("*"):
        if not f.is_file() or "__pycache__" in f.parts or f.suffix == ".pyc":
            continue
        rel = f.relative_to(src)
        tgt = dst / rel
        tgt.parent.mkdir(parents=True, exist_ok=True)
        if not tgt.exists() or md5(tgt) != md5(f):
            shutil.copy2(f, tgt)
            n += 1
    print(f"{_OK} {label}: 同步 {n} 个文件")
    return n


def sync() -> int:
    _sync_tree(FRONTEND.parent, PUB_FRONTEND.parent, "publish/frontend")
    _sync_tree(BACKEND_APP, PUB_BACKEND_APP, "publish/backend/app")
    return 0


def _extract_template_app(html: str) -> str | None:
    """取出 <div id="app"> 到其后第一个 <script> 之间的应用模板。"""
    m = re.search(r'<div\s+id="app"', html)
    if not m:
        return None
    s = m.start()
    nxt = html.find("<script", s)
    return html[s: nxt if nxt > 0 else len(html)]


def _defined_names(script: str) -> set[str]:
    """收集脚本里所有「可作为实例成员被调用/读取」的名字。

    宽松收集（宁可多收、不可漏收）：方法简写 `foo(){` / `async foo(){`、
    属性式 `foo: function(` / `foo: (`、data 与对象键 `foo:`、以及 const/let/function 声明。
    目的是**避免误报**，因此不做作用域区分 —— 只要名字在脚本里被定义过就算通过。
    """
    names: set[str] = set()
    # 方法简写（含 async / get / set 前缀）—— 行首锚定，避免把普通调用也算成定义
    names |= set(re.findall(r"^\s*(?:async\s+|get\s+|set\s+)?([A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
                            script, re.M))
    # 属性式定义：foo: function(){} / foo: () => {} / foo: async function(){}
    names |= set(re.findall(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*:\s*(?:async\s+)?(?:function\b|\()", script))
    # data 里的键 / 普通对象键（排除 `a: =` 这类比较与 `a: b` 的赋值误判由下面的负向断言处理）
    names |= set(re.findall(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*:(?![=:])", script))
    names |= set(re.findall(r"\b(?:const|let|var|function)\s+([A-Za-z_$][A-Za-z0-9_$]*)", script))
    return names


def _template_expressions(tpl: str) -> list[str]:
    """只取「会被当作 JS 求值」的片段，避免把普通文案（如 `ATR(14)`）误判成方法调用。"""
    exprs: list[str] = []
    exprs += [m.group(1) for m in re.finditer(r"\{\{([\s\S]*?)\}\}", tpl)]
    # 动态属性：v-if / v-else-if / v-show / v-for / @event / :prop
    attr = re.compile(r"""(?:^|\s)(?:v-if|v-else-if|v-show|v-for|v-model|@[\w:-]+|:[\w.-]+)\s*=\s*"([^"]*)\"""")
    exprs += [m.group(1) for m in attr.finditer(tpl)]
    return exprs


def _strip_strings(expr: str) -> str:
    """去掉字符串字面量 —— `style="transform:translateY(2px)"` 里的 translateY 不是函数调用。"""
    return re.sub(r"'[^'\n]*'|\"[^\"\n]*\"|`[^`]*`", "''", expr)


# 模板里合法但不由组件定义的调用（Vue 模板白名单 + 少量常用内建）
_TEMPLATE_BUILTINS = {
    "Math", "Date", "Number", "String", "Array", "Object", "JSON", "Boolean", "RegExp",
    "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent", "decodeURIComponent",
    "alert", "confirm", "prompt", "console", "setTimeout", "clearTimeout", "URLSearchParams",
    "new", "typeof", "in", "of", "return",
}


def check_template_refs(html_path: Path) -> None:
    """模板引用的方法/属性必须真实存在（并不得以 `_` 开头）。

    ## 为什么必须有这一条

    这是本项目**最危险的失败模式**：Vue 运行时编译下，模板里调用了不存在的方法
    （拼错名字，如模板写 `repNum()`、方法却叫 `reportNum()`）不会在编译期报错，
    而是在**渲染时**抛 `TypeError` → Vue 卸载整棵组件树 → 页面全白。
    用户看到的是「整个系统打不开」，而不是「一个单元格没值」，排查成本极高。
    历史上已因 `_` 前缀方法名踩过一次同类事故（Vue 把 `_`/`$` 前缀视为内部属性）。

    本检查只用于**兜底**：它靠正则近似解析，有误报就先补白名单，
    真正的验证仍应以「真浏览器渲染无报错」为准（见项目验证流程）。
    """
    if not html_path.exists():
        fail(f"{html_path.name} 不存在，无法做模板引用检查")
        return
    html = html_path.read_text(encoding="utf-8")

    tpl = _extract_template_app(html)
    if not tpl:
        fail("未找到 <div id=\"app\">，无法做模板引用检查")
        return

    # 主应用脚本 = 含 createApp 的**最长**那段（加载器脚本也会提到 createApp，不能只按「唯一」判断）
    segs = [m.group(1) for m in re.finditer(r"<script\b[^>]*>([\s\S]*?)</script>", html)
            if "createApp" in m.group(1)]
    if not segs:
        fail("未找到含 createApp 的主脚本，无法做模板引用检查")
        return
    script = max(segs, key=len)

    # v-for 别名不算方法调用（`v-for="c in list"` / `v-for="(row, i) in rows"`）
    aliases: set[str] = set()
    for m in re.finditer(r'v-for="\(?([^)"]*?)\)?\s+in\s', tpl):
        aliases |= {x.strip() for x in m.group(1).split(",") if x.strip()}

    calls: set[str] = set()
    for e in _template_expressions(tpl):
        calls |= set(re.findall(r"(?<![.\w$])([A-Za-z_$][A-Za-z0-9_$]*)\s*\(", _strip_strings(e)))

    defined = _defined_names(script)
    unknown = sorted(c for c in calls
                     if c not in defined and c not in _TEMPLATE_BUILTINS and c not in aliases)
    underscore = sorted(c for c in calls if c.startswith("_"))

    if unknown:
        fail(f"模板调用了不存在的方法/属性：{unknown}")
        print("        ⚠ 后果：Vue 运行时渲染抛 TypeError → 整页白屏（不是局部缺值）。")
        print("           请核对拼写，或把方法补到 methods/computed/data 中。")
    else:
        ok(f"模板引用的方法均存在（扫描 {len(calls)} 个调用）")

    if underscore:
        fail(f"模板调用了以 `_` 开头的方法：{underscore}")
        print("        ⚠ Vue 把 `_`/`$` 前缀视为组件内部属性，模板里取不到 → 同样整页白屏。")
    else:
        ok("模板未调用 `_` 前缀方法")


# ============ 检查 ============

def check(frontend_path: Path | None = None) -> int:
    fp = frontend_path or FRONTEND
    print("=" * 68)
    print("发版前一致性检查" + (f"（前端：{fp.name}）" if frontend_path else ""))
    print("=" * 68)

    # 1) 版本号：后端权威 → 前端常量一致
    bv, fv = read_backend_version(), read_frontend_version()
    if not bv:
        fail("backend/app/version.py 缺少 APP_VERSION")
    if not fv:
        fail("frontend/index.html 缺少 FRONTEND_VERSION 常量")
    if bv and fv:
        if bv == fv:
            ok(f"版本号一致：{bv}")
        else:
            fail(f"版本号不一致！后端 {bv} / 前端 {fv} → 运行 python scripts/release_check.py --bump")

    n = git_commit_count()
    if n and bv:
        try:
            vno = int(bv[-3:])
        except ValueError:
            vno = -1
        # 规则是「提交前填 N+1，提交后自洽」→ 提交前 vno==n+1、提交后 vno==n，两者都正常。
        if vno in (n, n + 1):
            ok(f"版本号与提交数自洽（{bv}，count={n}）")
        elif vno < n:
            warn(f"版本号 {bv}(序号{vno}) 落后于提交数 {n} —— 发版前请 --bump"
                 f"（预期 {(n + 1):03d}）")
        else:
            warn(f"版本号 {bv}(序号{vno}) 超前于提交数 {n} 超过 1 位，疑似手改过，请核对")

    # 2) 前端副本
    if not PUB_FRONTEND.exists():
        fail("publish/frontend/index.html 不存在")
    elif FRONTEND.exists():
        if md5(FRONTEND) == md5(PUB_FRONTEND):
            ok("publish/frontend/index.html 与 frontend/index.html 一致（md5）")
        else:
            fail("publish/frontend/index.html 与 frontend/index.html 不一致 → python scripts/release_check.py --sync")

    # 3) 后端副本（最易漏、后果最隐蔽）
    if not PUB_BACKEND_APP.exists():
        fail("publish/backend/app/ 不存在")
    else:
        missing, diff, same = [], [], 0
        for f in BACKEND_APP.rglob("*"):
            if not f.is_file() or "__pycache__" in f.parts or f.suffix == ".pyc":
                continue
            tgt = PUB_BACKEND_APP / f.relative_to(BACKEND_APP)
            if not tgt.exists():
                missing.append(str(f.relative_to(BACKEND_APP)))
            elif md5(tgt) != md5(f):
                diff.append(str(f.relative_to(BACKEND_APP)))
            else:
                same += 1
        # 反向检查：只在 publish 里存在的文件 —— 可能是部署专属文件（合法），
        # 也可能是已从源码删除的陈旧残留（会覆盖/干扰导入）。--sync 只增改不删，故只提示。
        src_names = {str(f.relative_to(BACKEND_APP)) for f in BACKEND_APP.rglob("*")
                     if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc"}
        pub_only = [str(f.relative_to(PUB_BACKEND_APP)) for f in PUB_BACKEND_APP.rglob("*")
                    if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc"
                    and str(f.relative_to(PUB_BACKEND_APP)) not in src_names]
        if pub_only:
            warn(f"publish/backend/app 存在源码中没有的文件 {len(pub_only)} 个（可能是部署专属或陈旧残留）："
                 f"{pub_only[:6]}")

        if not missing and not diff:
            ok(f"publish/backend/app 与 backend/app 一致（{same} 个文件）")
        else:
            msg = f"publish/backend/app 与 backend/app 不一致：{len(missing)} 缺失 / {len(diff)} 内容不同"
            fail(msg + " → python scripts/release_check.py --sync")
            for x in missing[:12]:
                print(f"        缺失: app/{x}")
            if len(missing) > 12:
                print(f"        … 另有 {len(missing) - 12} 个缺失")
            for x in diff[:12]:
                print(f"        不同: app/{x}")
            if len(diff) > 12:
                print(f"        … 另有 {len(diff) - 12} 个不同")
            print("        ⚠ 后果：前端已是新版、线上后端仍是旧版 → 新接口 404 / 新字段缺失，")
            print("           且多被前端静默降级吞掉，页面看不出异常。")

    # 4) 前端不应残留其它硬编码版本串（完整版本 = v + 8 位日期 + 3 位序号 = 12 字符）
    if fp.exists():
        txt = fp.read_text(encoding="utf-8")
        others = set(re.findall(r"v\d{11}", txt)) - ({fv} if fv else set())
        if others:
            warn(f"前端残留其它版本串 {sorted(others)}（应仅保留 FRONTEND_VERSION）")
        else:
            ok("前端无残留版本串")

    # 5) 模板引用的方法必须存在（否则整页白屏，见 check_template_refs 文档）
    check_template_refs(fp)

    print("-" * 68)
    if _problems:
        print(f"结论：❌ 不通过（{len(_problems)} 个问题，{len(_warnings)} 个警告）—— 请先修复再发版")
        return 1
    print(f"结论：✅ 通过（{len(_warnings)} 个警告）—— 可以发版")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="发版前一致性检查 / 版本号管理")
    ap.add_argument("--check", action="store_true", help="只检查（默认行为）")
    ap.add_argument("--bump", action="store_true", help="版本号升到「今天+提交数+1」并同步前端常量")
    ap.add_argument("--sync", action="store_true", help="同步 frontend/ 与 backend/app/ 到 publish/")
    ap.add_argument("--all", action="store_true", help="先 --sync 再 --check（发版标准流程）")
    ap.add_argument("--frontend", metavar="PATH", help="只对指定的前端 HTML 做检查（自测用）")
    a = ap.parse_args()

    rc = 0
    if a.bump:
        rc |= bump()
    if a.sync or a.all:
        rc |= sync()
    if a.check or a.all or not (a.bump or a.sync) or a.frontend:
        rc |= check(Path(a.frontend) if a.frontend else None)
    return rc


if __name__ == "__main__":
    sys.exit(main())
