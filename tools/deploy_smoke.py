#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
发版后冒烟自检：登录线上，核对核心数据接口非空，杜绝"发版后空白"被发现太晚。

用法：
    python tools/deploy_smoke.py
    MT_BASE_URL=https://xxx MT_USER=admin MT_PASS=baofu123 python tools/deploy_smoke.py

退出码：全部通过 0，任一失败 1。
"""
import json
import os
import sys
import urllib.request
import urllib.error

BASE = os.getenv("MT_BASE_URL", "https://d7f0dc809c4b4df981cd233488ed8876.app.workbuddy.link")
USER = os.getenv("MT_USER", "admin")
PASS = os.getenv("MT_PASS", "baofu123")

TIMEOUT = 25


def _post(path, payload):
    url = BASE + path
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(path, token):
    url = BASE + path
    req = urllib.request.Request(url, headers={
        "X-Auth-Token": token, "Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    print("=" * 60)
    print("milktea-trader 发版冒烟自检")
    print("BASE =", BASE)
    print("=" * 60)

    # 1) 登录
    try:
        login = _post("/api/auth/login", {"username": USER, "password": PASS})
    except Exception as e:
        print("✗ 登录失败：", e)
        return 1
    token = login.get("token")
    if not token:
        print("✗ 登录未返回 token：", login)
        return 1
    print("✓ 登录成功 (role=%s)" % login.get("user", {}).get("role", "?"))

    checks = []

    # 2) 可投池
    try:
        d = _get("/api/pool?page_size=1", token)
        total = d.get("total", 0)
        checks.append(("可投池 /api/pool", total > 0, "total=%s" % total))
    except Exception as e:
        checks.append(("可投池 /api/pool", False, "ERR %s" % e))

    # 3) 标签
    try:
        d = _get("/api/pool/tags", token)
        n = len(d) if isinstance(d, list) else 0
        checks.append(("标签 /api/pool/tags", n > 0, "count=%s" % n))
    except Exception as e:
        checks.append(("标签 /api/pool/tags", False, "ERR %s" % e))

    # 4) 访客记录
    try:
        d = _get("/api/auth/access-logs?page_size=5", token)
        total = d.get("total", 0)
        checks.append(("访客记录 /api/auth/access-logs", total >= 1, "total=%s" % total))
    except Exception as e:
        checks.append(("访客记录 /api/auth/access-logs", False, "ERR %s" % e))

    print("-" * 60)
    ok = True
    for name, passed, detail in checks:
        mark = "✓" if passed else "✗"
        print("  %s %-38s %s" % (mark, name, detail))
        ok = ok and passed
    print("-" * 60)
    if ok:
        print("结果：全部通过 ✅  核心数据均在，可放心交付。")
        return 0
    print("结果：存在失败 ❌  请勿在空白状态下交付，先回滚/排查。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
