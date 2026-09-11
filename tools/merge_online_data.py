# -*- coding: utf-8 -*-
"""
发版前：把线上用户数据拉回本地库合并（0911 实测踩坑后的保险动作）
==================================================================

为什么需要
----------
发布包 `publish/backend/data/app.db` 上传后**可能覆盖线上库**（机制未完全确定，
见项目记忆）。按最坏情况（会被覆盖）走 —— 合并是零成本的保险。

合并策略
--------
- **业务数据以线上为准**：缺失行 INSERT，重叠行用线上字段**整体覆盖**
  （不能只补缺失：本地常有测试遗留脏数据，只补缺失会让用户看到的数据"倒退"）。
- **不碰 users 表的凭据字段**（password_hash / salt / last_login_at）：
  覆盖密码有把本地改过的密码回退的风险，且登录时间本地通常更新。
- 用户匹配用 **username**（不能信线上 user_id，两边自增可能错位）。

覆盖的表
--------
  tracked_pool / pool_tags / position_rules / screens
  user_profile / user_settings / notify_config
  day_view_log / day_view_recap（不在 export-all 里，需逐只拉）

用法
----
    # 1) 先看差异（不写库）——发版前必看
    python tools/merge_online_data.py --dry-run

    # 2) 确认无误后真正合并
    python tools/merge_online_data.py

    # 3) 连盯盘日志一起拉（较慢，138 只约 1~2 分钟）
    python tools/merge_online_data.py --with-day-view

口令走环境变量 MT_PASS，脚本内无明文。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("MT_ONLINE", "https://d7f0dc809c4b4df981cd233488ed8876.app.workbuddy.link")
DB = os.environ.get("MT_DB", "backend/data/app.db")

# 表名 -> 业务唯一键（不含 user_id，user_id 由 username 映射后附加上去）
TABLE_KEYS = {
    "tracked_pool":   ["code"],
    "pool_tags":      ["name"],
    "position_rules": ["name"],
    "screens":        ["name"],
    "user_profile":   [],                 # 每用户一行
    "user_settings":  ["key"],
    "notify_config":  ["channel"],
    "day_view_log":   ["code", "trade_date"],
    "day_view_recap": ["code", "trade_date"],
}

# 这些字段永远由本地/数据库自己决定，不接受线上覆盖
IMMUTABLE = {"id", "user_id"}


def req(path, data=None, token=None, timeout=240):
    r = urllib.request.Request(
        BASE + path,
        data=json.dumps(data).encode() if data is not None else None,
        method="POST" if data is not None else "GET",
    )
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("X-Auth-Token", token)
    return json.loads(urllib.request.urlopen(r, timeout=timeout).read())


def login(pw: str) -> str:
    return req("/api/auth/login", {"username": "admin", "password": pw})["token"]


def table_columns(con, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def _norm(v):
    """归一化比较用：把 SQLite 的 0/1 与 JSON 的 False/True 视为相等，
    数值字符串与数字视为相等，dict/list 按 key 排序后序列化比较。

    不这么做的话，enabled: 1 vs True 会被判成差异 → 每次合并都产生无意义 UPDATE
    （实测：138 行池子里冒出 4 条假差异）。
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    s = str(v).strip()
    if s in ("true", "false"):
        return 1 if s == "true" else 0
    try:
        return float(s)
    except (TypeError, ValueError):
        return s


def _same(a, b) -> bool:
    na, nb = _norm(a), _norm(b)
    if na is None and nb is None:
        return True
    if na is None or nb is None:
        return False
    if isinstance(na, float) and isinstance(nb, float):
        return abs(na - nb) < 1e-9
    return na == nb


def diff_rows(con, table: str, uid: int, rows: list[dict], keys: list[str]):
    """对比线上 rows 与本地表，返回 (to_insert, to_update, only_local)。"""
    cols = set(table_columns(con, table))
    clean = []
    for r in rows:
        rr = {k: v for k, v in r.items() if k in cols and k not in IMMUTABLE}
        if not rr:
            continue
        clean.append(rr)
    if not clean:
        return [], [], []

    # ⚠ 只按 user_id 过滤，业务键在 Python 侧建索引。
    #   不要写成 WHERE user_id=? AND code=? 再传 [uid]*n —— 那会把 code 也传成 uid,
    #   导致「本地明明有 138 行却全部判为新增」(实测踩到)。
    all_cols = sorted(set().union(*(set(r) for r in clean))) if clean else []
    sel = ", ".join(["id"] + all_cols)
    local: dict[tuple, dict] = {}
    for row in con.execute(f"SELECT {sel} FROM {table} WHERE user_id = ?", [uid]):
        d = dict(zip(["id"] + all_cols, row))
        k = tuple(str(d.get(x, "")) for x in keys)
        local[k] = d

    to_insert, to_update = [], []
    seen = set()
    for rr in clean:
        k = tuple(str(rr.get(x, "")) for x in keys)
        seen.add(k)
        if k not in local:
            to_insert.append(rr)
        else:
            cur = local[k]
            changed = {kk: vv for kk, vv in rr.items()
                       if kk != "id" and not _same(cur.get(kk), vv)}
            if changed:
                to_update.append((cur["id"], changed))
    only_local = [k for k in local if k not in seen]
    return to_insert, to_update, only_local


def apply_rows(con, table: str, uid: int, to_insert, to_update):
    n_i = n_u = 0
    for rr in to_insert:
        cols = ["user_id"] + list(rr)
        ph = ",".join("?" * len(cols))
        con.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph})",
                    [uid] + [rr[c] for c in rr])
        n_i += 1
    for rid, changed in to_update:
        if not changed:
            continue
        sets = ",".join(f"{k} = ?" for k in changed)
        con.execute(f"UPDATE {table} SET {sets} WHERE id = ?", list(changed.values()) + [rid])
        n_u += 1
    return n_i, n_u


def fetch_day_view(codes: list[str], tok: str):
    """逐只拉盯盘日志与复盘，返回 (logs, recaps)。

    ⚠ 两个接口的返回形状不同，都不是 dict（早期实现按 dict 解析 → 静默得到 0 条,
      会让人误判「线上没有日志」而放心发版，实测踩到）：
      - GET /{code}/day-view-log → **list[DayViewLogOut]**
      - GET /{code}/watch-log    → dict{code, name, items:[{trade_date, recap, recap_user, ...}]}
        复盘(day_view_recap)没有独立的 GET 接口，只能从 watch-log 的 items 里取。
    """
    logs: list[dict] = []
    recaps: list[dict] = []
    errs: list[str] = []

    def one(code):
        out_l, out_r = [], []
        try:
            d = req(f"/api/pool/{code}/day-view-log", token=tok, timeout=60)
            if isinstance(d, list):
                for it in d:
                    it = dict(it)
                    it.setdefault("code", code)
                    out_l.append(it)
            elif isinstance(d, dict):
                for it in (d.get("logs") or d.get("items") or []):
                    it = dict(it)
                    it.setdefault("code", code)
                    out_l.append(it)
        except Exception as e:
            errs.append(f"{code} log: {e}")
        try:
            w = req(f"/api/pool/{code}/watch-log", token=tok, timeout=60)
            items = (w or {}).get("items") or [] if isinstance(w, dict) else []
            for it in items:
                if it.get("recap"):
                    out_r.append({"code": code, "trade_date": it.get("trade_date"),
                                  "recap": it.get("recap"),
                                  "operator": it.get("recap_user") or ""})
        except Exception as e:
            errs.append(f"{code} recap: {e}")
        return out_l, out_r

    with ThreadPoolExecutor(max_workers=8) as ex:
        for l, r in ex.map(one, codes):
            logs.extend(l)
            recaps.extend(r)
    return logs, recaps, errs


def main():
    ap = argparse.ArgumentParser(description="发版前：线上用户数据合并回本地库")
    ap.add_argument("--dry-run", action="store_true", help="只报告差异，不写库")
    ap.add_argument("--with-day-view", action="store_true", help="连盯盘日志/复盘一起拉")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    pw = os.environ.get("MT_PASS", "")
    if not pw:
        raise SystemExit("[FATAL] 请设置环境变量 MT_PASS（脚本内不写明文口令）")

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    # username -> 本地 user_id
    umap = {r[0]: r[1] for r in con.execute("SELECT username, id FROM users")}
    if not umap:
        raise SystemExit("[FATAL] 本地库没有 users 表/数据")

    print("=" * 72)
    print("线上用户数据合并" + ("（DRY-RUN，不写库）" if args.dry_run else "（写入模式）"))
    print("=" * 72)
    tok = login(pw)
    h = req("/api/health")
    print(f"线上版本: {h.get('version')}")
    exp = req("/api/data/export-all", token=tok)
    users = exp.get("users") or {}
    print(f"线上用户: {', '.join(users) or '无'}\n")

    total_i = total_u = 0
    for uname, info in users.items():
        uid = umap.get(uname)
        if uid is None:
            print(f"[跳过] 线上用户 {uname} 在本地库不存在（不自动建号，避免 user_id 错位）")
            continue
        data = info.get("data") or {}
        print(f"── {uname} (本地 user_id={uid}) " + "─" * 40)
        for table, keys in TABLE_KEYS.items():
            if table in ("day_view_log", "day_view_recap"):
                continue
            rows = data.get(table) or []
            if not rows:
                continue
            ti, tu, only = diff_rows(con, table, uid, rows, keys)
            mark = "   " if not (ti or tu) else " * "
            print(f"{mark}{table:16s} 线上 {len(rows):>4} 行 → 新增 {len(ti):>3} 更新 {len(tu):>3}"
                  + (f"  本地独有 {len(only)}" if only else ""))
            if not args.dry_run and (ti or tu):
                n_i, n_u = apply_rows(con, table, uid, ti, tu)
                total_i += n_i
                total_u += n_u
            else:
                total_i += len(ti)
                total_u += len(tu)

    # ---- 盯盘日志（不在 export-all 内） ----
    if args.with_day_view:
        for uname, info in users.items():
            uid = umap.get(uname)
            if uid is None:
                continue
            rows = (info.get("data") or {}).get("tracked_pool") or []
            codes = [r["code"] for r in rows if r.get("code")]
            print(f"\n── {uname} 盯盘日志（{len(codes)} 只）" + "─" * 20)
            logs, recaps, errs = fetch_day_view(codes, tok)
            if errs:
                print(f"   拉取失败 {len(errs)} 项（示例: {errs[0][:80]}）")
            logs = [r for r in logs if r.get("trade_date")]
            recaps = [r for r in recaps if r.get("trade_date")]
            for table, subset in (("day_view_log", logs), ("day_view_recap", recaps)):
                if not subset:
                    print(f"   {table:16s} 线上 0 条")
                    continue
                ti, tu, only = diff_rows(con, table, uid, subset, keys)
                print(f"   {table:16s} 线上 {len(subset):>4} 行 → 新增 {len(ti):>3} 更新 {len(tu):>3}")
                if not args.dry_run and (ti or tu):
                    n_i, n_u = apply_rows(con, table, uid, ti, tu)
                    total_i += n_i
                    total_u += n_u
                else:
                    total_i += len(ti)
                    total_u += len(tu)

    print("\n" + "=" * 72)
    if args.dry_run:
        print(f"DRY-RUN 汇总：将新增 {total_i} 行、更新 {total_u} 行（未写库）")
        print("确认无误后去掉 --dry-run 再跑一次。")
    else:
        con.commit()
        print(f"已写入：新增 {total_i} 行、更新 {total_u} 行")
    con.close()


if __name__ == "__main__":
    sys.exit(main())
