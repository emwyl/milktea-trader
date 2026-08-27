"""全 A 股行业字段补全(申万一级):多源 fallback。
优先级: akshare sw_index_third_cons (申万三级 → 一级) → akshare 东财全市场 → 静态预置。
返回: {added, updated, total, source, error}
"""
from __future__ import annotations
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.db import SessionLocal
from app.models import Stock

# 申万一级行业硬编码兜底(2021 版共 31 个),数据源全断时仍能给前端做行业 chip
SW_FIRST_LEVELS = [
    "农林牧渔", "基础化工", "钢铁", "有色金属", "电子", "汽车", "家用电器",
    "食品饮料", "纺织服饰", "轻工制造", "医药生物", "公用事业", "交通运输",
    "房地产", "商贸零售", "社会服务", "银行", "非银金融", "建筑材料", "建筑装饰",
    "电力设备", "机械设备", "国防军工", "煤炭", "石油石化", "环保", "美容护理",
    "计算机", "传媒", "综合", "电子设备", "通信",
]


def _fetch_from_sw() -> dict[str, str] | None:
    """akshare 申万三级 → 一级映射。legulegu 不可达时返回 None。"""
    try:
        import akshare as ak
    except Exception:
        return None
    try:
        df3 = ak.sw_index_third_info()
    except Exception:
        return None
    if df3 is None or len(df3) < 10:
        return None

    def fetch_one(row):
        try:
            comp = ak.sw_index_third_cons(symbol=row['行业代码'])
            codes = [str(s).split('.')[0] for s in comp['股票代码'].tolist() if s]
            return row['上级行业'], codes
        except Exception:
            return None

    mapping: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(fetch_one, row) for _, row in df3.iterrows()]
        for f in as_completed(futs):
            r = f.result()
            if not r:
                continue
            parent, codes = r
            for c in codes:
                if c not in mapping:
                    mapping[c] = parent
    return mapping if len(mapping) > 1000 else None  # 太少视为失败


def _fetch_from_eastmoney() -> dict[str, str] | None:
    """akshare 东财全市场快照(含 industry 字段)。沙箱常被拒。"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
    except Exception:
        return None
    if df is None or len(df) < 1000:
        return None
    mapping = {}
    for _, row in df.iterrows():
        code = str(row.get('代码', '')).strip()
        ind = str(row.get('行业', '')).strip()
        if code and ind and ind != 'nan':
            mapping[code] = ind
    return mapping if mapping else None


def seed_industries() -> dict:
    """补全 stocks.industry 字段。返回 {added, updated, total, source, error}。"""
    mapping = _fetch_from_sw() or _fetch_from_eastmoney()
    if not mapping:
        return {"added": 0, "updated": 0, "total": 0, "source": "none",
                "error": "所有数据源不可达(申万 legulegu + 东财全市场都被拒),可稍后重试"}

    db = SessionLocal()
    try:
        added = updated = 0
        for code, industry in mapping.items():
            s = db.get(Stock, code)
            if s:
                if (s.industry or '') != industry:
                    s.industry = industry
                    updated += 1
            else:
                # 不存在的 code 跳过(可能 akshare 给的 code 不在 stocks 表里,比如新上市的)
                continue
        db.commit()
        total = db.query(Stock).filter(Stock.industry != '').count()
        return {"added": added, "updated": updated, "total": total,
                "source": "sw" if _fetch_from_sw else "eastmoney", "error": ""}
    finally:
        db.close()


def get_first_level_industries() -> list[str]:
    """返回申万一级行业列表(供前端做行业 chip 多选)。

    只返回申万 32 个标准一级(2021 版),DB 里的实际行业可能零散、不标准。
    用户选了某个行业后,等 seed-industries 把数据补全,就会有真实股票匹配。
    """
    return list(SW_FIRST_LEVELS)
