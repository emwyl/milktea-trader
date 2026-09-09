#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v185: 批量初始化 stocks.industry(行业)。

背景
----
板块共振(C 类 4 分)依赖 stocks.industry,但库里 5549 只只有 44 只有值,
导致板块共振长期显示「数据缺失/不计分」。行业变动频率低,适合一次性初始化入库。

数据源
------
东方财富 datacenter RPT_F10_BASIC_ORGINFO:
  - EM2016      东财行业三级,如 "金融-银行-股份制与城商行"  → 取二级 "银行"
  - INDUSTRYCSRC1 证监会行业,如 "金融业-货币金融服务"        → 兜底取末段
(东财 push2 f127 在本机代理环境常被限流,datacenter 更稳)

用法
----
  python tools/init_industry.py                 # 只补 industry 为空的股票(推荐)
  python tools/init_industry.py --all           # 全量覆盖(统一行业口径)
  python tools/init_industry.py --limit 200     # 只处理前 N 只(调试)
  python tools/init_industry.py --db path/app.db
  python tools/init_industry.py --dry-run       # 只打印不写库
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
PAGE_SIZE = 500


def norm_industry(em2016: str = "", csrc1: str = "") -> str:
    """东财三级行业 → 二级。详见 data_fetcher._norm_industry(同口径)。"""
    s = (em2016 or "").strip()
    if s:
        parts = [x.strip() for x in s.split("-") if x.strip()]
        if len(parts) >= 2:
            return parts[1]
        if parts:
            return parts[0]
    s = (csrc1 or "").strip()
    if s:
        parts = [x.strip() for x in s.split("-") if x.strip()]
        if parts:
            return parts[-1]
    return ""


def fetch_page(page: int) -> list[dict]:
    """拉一页全市场行业数据(最多 500 条)。失败返回 []。"""
    params = {
        "reportName": "RPT_F10_BASIC_ORGINFO",
        "columns": "SECUCODE,SECURITY_CODE,EM2016,INDUSTRYCSRC1",
        "pageSize": PAGE_SIZE,
        "pageNumber": page,
        "sortColumns": "SECURITY_CODE",
        "sortTypes": "1",
    }
    for attempt in (1, 2, 3):
        try:
            with httpx.Client(headers={"User-Agent": UA,
                                       "Referer": "https://data.eastmoney.com/"},
                              timeout=15.0, follow_redirects=True) as cli:
                r = cli.get(API, params=params)
            if r.status_code != 200:
                continue
            rows = (((r.json() or {}).get("result") or {}).get("data")) or []
            return rows
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] page {page} 第 {attempt} 次失败: {type(e).__name__}", file=sys.stderr)
            time.sleep(1.0 * attempt)
    return []


def fetch_all(max_pages: int = 60) -> dict[str, str]:
    """并发拉全量,返回 {6位代码: 二级行业}。"""
    out: dict[str, str] = {}
    first = fetch_page(1)
    if not first:
        print("[error] 首页拉取失败,可能网络不可达或被限流", file=sys.stderr)
        return out
    def _take(rows):
        for it in rows:
            code = str(it.get("SECURITY_CODE") or "").strip()
            ind = norm_industry(it.get("EM2016"), it.get("INDUSTRYCSRC1"))
            if code and ind:
                out[code] = ind
    _take(first)
    print(f"  第 1 页: {len(first)} 条,累计 {len(out)}")
    pages = list(range(2, max_pages + 1))
    with ThreadPoolExecutor(max_workers=6) as ex:
        for rows in ex.map(fetch_page, pages):
            if not rows:
                continue
            _take(rows)
            if len(rows) < PAGE_SIZE:
                break
    print(f"  共拉取行业映射 {len(out)} 条")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="", help="sqlite 路径,默认自动定位 backend/data/app.db")
    ap.add_argument("--all", action="store_true", help="全量覆盖已有行业(统一口径)")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写库")
    args = ap.parse_args()

    db_path = args.db
    if not db_path:
        here = os.path.dirname(os.path.abspath(__file__))
        for cand in (
            os.path.join(here, "..", "publish", "backend", "data", "app.db"),
            os.path.join(here, "..", "backend", "data", "app.db"),
            os.path.join(here, "..", "publish", "backend", "app.db"),
        ):
            cand = os.path.normpath(cand)
            if os.path.exists(cand):
                db_path = cand
                break
    if not db_path or not os.path.exists(db_path):
        print(f"[error] 找不到数据库: {db_path}", file=sys.stderr)
        return 2
    print(f"数据库: {db_path}")

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    if args.all:
        cur.execute("SELECT code, name, industry FROM stocks")
    else:
        cur.execute("SELECT code, name, industry FROM stocks "
                    "WHERE industry IS NULL OR TRIM(industry)=''")
    targets = [(str(c), n or "", i or "") for c, n, i in cur.fetchall()]
    if args.limit:
        targets = targets[: args.limit]
    print(f"待处理股票: {len(targets)} 只({'全量覆盖' if args.all else '仅补空'})")
    if not targets:
        con.close()
        return 0

    print("拉取东方财富行业映射 …")
    t0 = time.time()
    ind_map = fetch_all()
    print(f"  耗时 {time.time() - t0:.1f}s")

    hit = miss = 0
    updates: list[tuple[str, str]] = []
    for code, name, old in targets:
        ind = ind_map.get(code, "")
        if ind:
            hit += 1
            if args.all or ind != old:
                updates.append((ind, code))
        else:
            miss += 1
    print(f"命中 {hit} 只 / 未命中 {miss} 只,需写库 {len(updates)} 条")

    if updates and not args.dry_run:
        cur.executemany("UPDATE stocks SET industry=? WHERE code=?", updates)
        con.commit()
        print(f"已写入 {len(updates)} 条")
    elif args.dry_run:
        print("(dry-run) 未写库")

    # 复核覆盖率
    cur.execute("SELECT COUNT(*) FROM stocks")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM stocks WHERE industry IS NOT NULL AND TRIM(industry)<>''")
    filled = cur.fetchone()[0]
    print(f"覆盖率: {filled}/{total} ({filled * 100.0 / max(total, 1):.1f}%)")

    # 池内覆盖率(真正影响板块共振的范围)
    try:
        cur.execute("SELECT COUNT(DISTINCT tp.code) FROM tracked_pool tp")
        p_total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT tp.code) FROM tracked_pool tp "
                    "JOIN stocks s ON s.code=tp.code "
                    "WHERE s.industry IS NOT NULL AND TRIM(s.industry)<>''")
        p_filled = cur.fetchone()[0]
        print(f"可投池覆盖率: {p_filled}/{p_total}")
        print("--- 池内行业分布(TOP15) ---")
        cur.execute("SELECT s.industry, COUNT(*) c FROM tracked_pool tp "
                    "JOIN stocks s ON s.code=tp.code "
                    "WHERE s.industry IS NOT NULL AND TRIM(s.industry)<>'' "
                    "GROUP BY s.industry ORDER BY c DESC LIMIT 15")
        for ind, c in cur.fetchall():
            print(f"  {ind}: {c}")
    except Exception as e:  # noqa: BLE001
        print(f"(池内统计跳过: {e})")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
