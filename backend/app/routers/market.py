"""市场行情补充路由：股指/外盘期货快照（新浪，需后端代理伪造 Referer）。

背景：
- 腾讯 qt.gtimg.cn / web.ifzq.gtimg.cn 不支持期货代码（nf_IF0、XIN9 都返回 pv_none_match）。
- 新浪 hq.sinajs.cn 有期货数据，但做了防盗链：不带 `Referer: https://finance.sina.com.cn`
  直接返回 `Forbidden`。浏览器改不了自己页面的 Referer，所以只能由后端代拉。
"""
from __future__ import annotations

import re
import time
from typing import Dict, List

import requests
from fastapi import APIRouter, Depends

from app.deps import get_current_user
from app.models import User

router = APIRouter(prefix="/api/market", tags=["market"])

SINA_URL = "https://hq.sinajs.cn/list={codes}"
HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}

# 内置可选期货（code 为新浪代码）。前端配置弹窗里从这里挑。
FUTURES_PRESETS: List[Dict[str, str]] = [
    {"code": "hf_CHA50CFD", "name": "富时A50期货", "note": "SGX 富时中国A50 期指连续"},
    {"code": "nf_IF0", "name": "IF沪深300期货", "note": "中金所沪深300 股指期货连续"},
    {"code": "nf_IH0", "name": "IH上证50期货", "note": "中金所上证50 股指期货连续"},
    {"code": "nf_IC0", "name": "IC中证500期货", "note": "中金所中证500 股指期货连续"},
    {"code": "nf_IM0", "name": "IM中证1000期货", "note": "中金所中证1000 股指期货连续"},
]

_CACHE: Dict[str, dict] = {"ts": 0.0, "data": {}}
_CACHE_TTL = 15.0  # 秒


def _num(v):
    try:
        f = float(v)
        return f if f == f else None  # 过滤 NaN
    except Exception:
        return None


def _parse_hf(arr: List[str]) -> dict:
    """外盘期货 hf_ 字段：
    0=最新价 2=买价 3=卖价 4=最高 5=最低 6=时间 7=昨收 8=开盘
    9=持仓量 12=日期 13=名称 14=成交量
    """
    last = _num(arr[0]) if len(arr) > 0 else None
    prev = _num(arr[7]) if len(arr) > 7 else None
    if prev is None:
        prev = _num(arr[8]) if len(arr) > 8 else None
    if last is None:
        return {}
    chg = (last - prev) if prev else 0.0
    pct = (chg / prev * 100) if prev else 0.0
    return {
        "price": last,
        "prev_close": prev,
        "chg": chg,
        "chg_pct": pct,
        "open": _num(arr[8]) if len(arr) > 8 else None,
        "high": _num(arr[4]) if len(arr) > 4 else None,
        "low": _num(arr[5]) if len(arr) > 5 else None,
        "date": arr[12] if len(arr) > 12 else "",
        "time": arr[6] if len(arr) > 6 else "",
        "name": arr[13] if len(arr) > 13 else "",
    }


def _parse_nf(arr: List[str]) -> dict:
    """内盘期货 nf_ 字段（已用 IF0/IC0 的日K交叉核对）：
    0=开盘 1=最高 2=最低 3=今收 4=成交量 5=成交额 6=持仓量 7=最新价
    13=**昨收盘** 15=持仓量 末尾: 日期 时间 … 名称
    注意：昨收不是 idx3，idx3 在收盘后与最新价相同，直接用会算出 0.00% 的假涨幅。
    """
    last = _num(arr[7]) if len(arr) > 7 else None
    if last is None:
        last = _num(arr[3]) if len(arr) > 3 else None
    prev = _num(arr[13]) if len(arr) > 13 else None
    if last is None:
        return {}
    if not prev:
        prev = last
    chg = last - prev
    pct = (chg / prev * 100) if prev else 0.0
    date, tm = "", ""
    # 日期形如 2026-09-04，时间形如 15:00:00
    for i, v in enumerate(arr):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v or ""):
            date = v
            if len(arr) > i + 1 and re.fullmatch(r"\d{2}:\d{2}:\d{2}", arr[i + 1] or ""):
                tm = arr[i + 1]
            break
    return {
        "price": last,
        "prev_close": prev,
        "chg": chg,
        "chg_pct": pct,
        "open": _num(arr[0]) if len(arr) > 0 else None,
        "high": _num(arr[1]) if len(arr) > 1 else None,
        "low": _num(arr[2]) if len(arr) > 2 else None,
        "date": date,
        "time": tm,
        "name": arr[-1] if arr else "",
    }


def _fetch(codes: List[str]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if not codes:
        return out
    try:
        r = requests.get(SINA_URL.format(codes=",".join(codes)), headers=HEADERS, timeout=8)
        r.encoding = "gbk"
        text = r.text
    except Exception as e:
        return {"_error": {"msg": f"上游不可达: {type(e).__name__}"}}
    for code in codes:
        m = re.search(r'var hq_str_' + re.escape(code) + r'="([^"]*)"', text)
        if not m or not m.group(1).strip():
            continue
        arr = m.group(1).split(",")
        rec = _parse_hf(arr) if code.startswith("hf_") else _parse_nf(arr)
        if rec:
            rec["code"] = code
            out[code] = rec
    return out


@router.get("/futures-presets")
def futures_presets(user: User = Depends(get_current_user)):
    """返回可选的期货清单（供前端配置弹窗使用）。"""
    return {"items": FUTURES_PRESETS}


@router.get("/futures")
def futures(codes: str = "", user: User = Depends(get_current_user)):
    """按新浪代码批量取期货快照。codes 逗号分隔，如 hf_CHA50CFD,nf_IF0。"""
    code_list = [c.strip() for c in (codes or "").split(",") if c.strip()]
    if not code_list:
        return {"quotes": {}, "ts": int(time.time())}
    # 15 秒缓存，避免自动刷新把上游打爆
    now = time.time()
    cached = _CACHE.get("data") or {}
    if now - float(_CACHE.get("ts") or 0) < _CACHE_TTL and all(c in cached or c in (_CACHE.get("miss") or []) for c in code_list):
        return {"quotes": {c: cached[c] for c in code_list if c in cached}, "ts": int(now)}
    data = _fetch(code_list)
    err = data.pop("_error", None)
    if not err:
        _CACHE["ts"] = now
        _CACHE["data"] = data
    return {"quotes": data, "ts": int(now), "error": (err or {}).get("msg")}
