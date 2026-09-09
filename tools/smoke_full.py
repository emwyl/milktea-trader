#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
发版后「全量人工模拟」冒烟自检 v2 —— 在 deploy_smoke.py 基础上扩展，
逐项核对用户真实录入的数据是否还在，而不只是"接口通"。

覆盖：
  1. 登录（admin）
  2. 可投池证券（逐只列出 代码/名称/日初判断/日初日志数/盯盘日志数）
  3. 日初判断（day_view_today 非空数 + 抽 1 只回查 day-view-log 明细）
  4. 自定义规则（条数 + 风险等级映射 + 风控提示 risk_notice）
  5. 标签（条数 + 明细）
  6. 盯盘日志（每只池内证券的 watch-log 条数汇总）
  7. 首页风控提示 /api/settings/risk_notice
  8. 引擎信号（rule_name / rule_risk_level / rule_notice 三字段，v173 新增）
  9. 前端版本号（HTML 里的 appVersion）

用法：
    python tools/smoke_full.py
    MT_BASE_URL=https://xxx MT_USER=admin MT_PASS=xxx python tools/smoke_full.py

退出码：全部通过 0，任一失败 1。
"""
import json
import os
import sys
import urllib.request
import urllib.error

BASE = os.getenv("MT_BASE_URL",
                 "https://8000-d7f0dc809c4b4df981cd233488ed8876.e2b.bj2.sandbox.cloudstudio.club")
USER = os.getenv("MT_USER", "admin")
PASS = os.getenv("MT_PASS", "baofu123")
TIMEOUT = 40

CHECKS = []


def add(name, passed, detail=""):
    CHECKS.append((name, bool(passed), detail))


def _req(path, method="GET", payload=None, token=None, raw=False):
    url = BASE + path
    headers = {}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Auth-Token"] = token
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        body = r.read().decode("utf-8")
        return body if raw else json.loads(body)


def get(path, token, raw=False):
    return _req(path, "GET", token=token, raw=raw)


def post(path, payload, token=None):
    return _req(path, "POST", payload=payload, token=token)


def fetch_pool_all(token):
    """分页拉全量可投池（page_size 有上限，超过会 422）"""
    items, page, per = [], 1, 50
    while True:
        d = get("/api/pool?page=%s&page_size=%s" % (page, per), token)
        batch = d.get("items", [])
        items.extend(batch)
        total = d.get("total", 0)
        if not batch or len(items) >= total:
            break
        page += 1
        if page > 40:
            break
    return items


def level_of(priority):
    """priority -> 风险等级（与前端 riskLevelOf 一致）"""
    try:
        p = int(priority)
    except Exception:
        return "关注"
    return "紧急" if p >= 8 else ("重要" if p >= 5 else "关注")


def main():
    print("=" * 72)
    print("milktea-trader 全量冒烟自检（模拟人工验收）")
    print("BASE =", BASE)
    print("=" * 72)

    # ---------- 1) 登录 ----------
    try:
        login = post("/api/auth/login", {"username": USER, "password": PASS})
        token = login.get("token")
        if not token:
            add("登录", False, "未返回 token: %s" % login)
            return report()
        me = get("/api/auth/me", token)
        add("登录", True, "user=%s role=%s" % (me.get("username"), me.get("role")))
    except Exception as e:
        add("登录", False, "ERR %s" % e)
        return report()

    pool_codes = []

    # ---------- 2) 可投池 ----------
    try:
        items = fetch_pool_all(token)
        add("可投池证券", len(items) > 0, "共 %s 只" % len(items))
        print("-" * 72)
        print("  %-8s %-10s %-8s %-8s %s" % ("代码", "名称", "日初判断", "日初日志", "标签"))
        for it in items:
            pool_codes.append(it.get("code"))
            print("  %-8s %-10s %-8s %-8s %s" % (
                it.get("code"), it.get("name"),
                (it.get("day_view_today") or "-"),
                it.get("day_view_log_count", 0),
                ",".join(t.get("name", "") for t in (it.get("tags") or [])) or "-"))
        print("-" * 72)
    except Exception as e:
        add("可投池证券", False, "ERR %s" % e)

    # ---------- 3) 日初判断 ----------
    try:
        items = fetch_pool_all(token)
        filled = [i for i in items if (i.get("day_view_today") or "").strip()]
        logged = [i for i in items if (i.get("day_view_log_count") or 0) > 0]
        # 日初判断是"当日"字段，跨交易日会自动归零待重填，因此只作提示、不判失败；
        # 真正的"数据是否还在"看 day_view_log_count（历史记录）
        add("日初判断(今日)", True,
            "%s/%s 只已填%s" % (len(filled), len(items),
                              "" if filled else "（新交易日待填，属正常）"))
        add("日初判断日志", len(logged) > 0, "%s 只有历史记录" % len(logged))
        # 抽 1 只回查明细
        if logged:
            c = logged[0]["code"]
            lg = get("/api/pool/%s/day-view-log" % c, token)
            n = len(lg) if isinstance(lg, list) else len(lg.get("items", []))
            add("日初判断明细回查(%s)" % c, n > 0, "%s 条" % n)
    except Exception as e:
        add("日初判断", False, "ERR %s" % e)

    # ---------- 4) 自定义规则 ----------
    try:
        rules = get("/api/rules", token)
        rules = rules if isinstance(rules, list) else rules.get("items", [])
        add("自定义规则", len(rules) > 0, "共 %s 条" % len(rules))
        for r in rules:
            lv = level_of(r.get("priority"))
            print("   #%-3s %-22s 等级=%-4s 启用=%-5s 风控提示=%s" % (
                r.get("id"), r.get("name"), lv, r.get("enabled"),
                (r.get("risk_notice") or "-")[:24]))
        has_notice = sum(1 for r in rules if (r.get("risk_notice") or "").strip())
        add("规则·风控提示字段", True, "%s/%s 条已录入提示" % (has_notice, len(rules)))
        add("规则·风险等级映射", True,
            "紧急%s/重要%s/关注%s" % (
                sum(1 for r in rules if level_of(r.get("priority")) == "紧急"),
                sum(1 for r in rules if level_of(r.get("priority")) == "重要"),
                sum(1 for r in rules if level_of(r.get("priority")) == "关注")))
    except Exception as e:
        add("自定义规则", False, "ERR %s" % e)

    # ---------- 5) 标签 ----------
    try:
        tags = get("/api/pool/tags", token)
        tags = tags if isinstance(tags, list) else tags.get("items", [])
        add("标签", len(tags) > 0,
            "%s 个: %s" % (len(tags), ",".join(t.get("name", "?") for t in tags)))
    except Exception as e:
        add("标签", False, "ERR %s" % e)

    # ---------- 6) 盯盘日志 ----------
    try:
        tot = 0
        detail = []
        for c in pool_codes[:20]:
            try:
                w = get("/api/pool/%s/watch-log" % c, token)
                n = len(w) if isinstance(w, list) else len(w.get("items", []))
                if n:
                    tot += n
                    detail.append("%s:%s" % (c, n))
            except Exception:
                pass
        add("盯盘日志", tot > 0, "共 %s 条 (%s)" % (tot, " ".join(detail[:8])))
    except Exception as e:
        add("盯盘日志", False, "ERR %s" % e)

    # ---------- 7) 首页风控提示 ----------
    try:
        rn = get("/api/settings/risk_notice", token)
        content = rn.get("content", "") if isinstance(rn, dict) else str(rn)
        add("首页风控提示接口", True,
            "已录入 %s 字: %s" % (len(content), content[:30] or "(空)"))
    except Exception as e:
        add("首页风控提示接口", False, "ERR %s" % e)

    # ---------- 8) 引擎信号（v173 新字段） ----------
    try:
        sigs = get("/api/engine/signals?limit=50", token)
        sigs = sigs if isinstance(sigs, list) else (sigs.get("items") or sigs.get("signals") or [])
        add("引擎信号", len(sigs) > 0, "共 %s 条" % len(sigs))
        if sigs:
            s0 = sigs[0]
            has3 = all(k in s0 for k in ("rule_name", "rule_risk_level", "rule_notice"))
            add("信号·规则三字段(v173)", has3,
                "rule_name=%s / level=%s" % (s0.get("rule_name"), s0.get("rule_risk_level")))
            named = sum(1 for s in sigs if (s.get("rule_name") or "").strip())
            add("信号·规则名称回填", named > 0, "%s/%s 条有规则名" % (named, len(sigs)))
    except Exception as e:
        add("引擎信号", False, "ERR %s" % e)

    # ---------- 9) 前端版本 ----------
    try:
        html = get("/", token, raw=True)
        ver = "?"
        if 'id="appVersion"' in html:
            seg = html.split('id="appVersion"', 1)[1][:80]
            ver = seg.split(">", 1)[1].split("<", 1)[0].strip()
        add("前端版本", ver not in ("?", ""), "appVersion=%s" % ver)
    except Exception as e:
        add("前端版本", False, "ERR %s" % e)

    return report()


def report():
    print("-" * 72)
    ok = True
    for name, passed, detail in CHECKS:
        print("  %s %-30s %s" % ("✓" if passed else "✗", name, detail))
        ok = ok and passed
    print("-" * 72)
    print("结果：%s" % ("全部通过 ✅" if ok else "存在失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
