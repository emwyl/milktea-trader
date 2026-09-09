"""启动前行情库自愈（部署用）。

背景(2026-09-08)：沙箱运行库 app.db 不会被重新部署覆盖；历史故障期旧代码写入的
demo 假价行(8~45 随机游走,成交量带小数)残留在运行库里,导致线上多股 mkt.q=0「数据异常」。
本脚本在 uvicorn 启动前把随包上传的干净日线种子 quotes.seed 合并进运行库：
对种子中每个代码,仅当运行库「无该代码 / 最新日期早于种子 / 存在成交量带小数(demo假行)」
时才整段替换该代码日线 —— 保留运行库中更新的真实行,且绝不触碰 users/pool 等业务表。
"""
import os
import sqlite3

base = os.path.dirname(os.path.abspath(__file__))  # publish/backend/
MAIN_DB = os.path.join(base, "data", "app.db")
SEED_FILE = os.path.join(base, "quotes.seed")


def _healthy_enough():
    """快速判断运行库是否已足够健康 —— 健康则直接跳过合并(秒级)。

    2026-09-08 故障复盘:旧版每次启动都对种子(21万行)+主库(31万行)做无索引的
    分组扫描,在沙箱慢盘上耗时数分钟且全程持有写锁,把需要写库的 /api/auth/login
    一并堵死(表现为部署后数分钟登录超时)。因此先做低成本快检,能跳就跳。
    """
    if not os.path.exists(MAIN_DB):
        return False
    con = sqlite3.connect(MAIN_DB, timeout=5)
    try:
        con.execute("PRAGMA busy_timeout = 3000")
        total = con.execute("SELECT COUNT(*) FROM daily_quotes").fetchone()[0]
        if total < 150000:
            return False
        mx = con.execute("SELECT MAX(date) FROM daily_quotes").fetchone()[0] or ""
        if mx < _recent_floor():      # 数据严重滞后 → 需要合并
            return False
        # 只检查「池内可见代码」是否残留 demo 假行 —— 非池内代码(如 999999 测试行)的
        # 历史脏数据不影响用户看到的「数据异常」, 且种子里本就没有它们、合并也修不掉,
        # 若把它们算进来会导致每次启动都白跑一次全量扫描。
        try:
            demo = con.execute(
                """
                SELECT 1 FROM daily_quotes d
                 WHERE d.code IN (SELECT code FROM tracked_pool)
                   AND d.volume != CAST(d.volume AS INTEGER) LIMIT 1
                """
            ).fetchone()
        except Exception:
            # tracked_pool 表不存在等异常 → 保守:认为不健康,走一次合并
            return False
        return demo is None
    except Exception as e:
        print("SEED health check error:", e)
        return False
    finally:
        con.close()


def _recent_floor(days=10):
    """返回 (今天 - days) 的 YYYY-MM-DD,作为'数据是否算新'的门槛。"""
    import datetime

    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def main():
    if not os.path.exists(SEED_FILE):
        print("SEED absent, skip")
        return
    if _healthy_enough():
        print("SEED skip: main db healthy (>=150k rows, fresh, no demo rows)")
        return
    con = None
    try:
        con = sqlite3.connect(MAIN_DB, timeout=15)
        con.execute("PRAGMA busy_timeout = 15000")   # 拿不到锁最多等 15s,不无限阻塞启动
        con.execute("ATTACH DATABASE ? AS seed_db", (SEED_FILE,))
        con.execute("DROP TABLE IF EXISTS _heal")
        con.execute(
            """
            CREATE TEMP TABLE _heal AS
              SELECT s.code
                FROM (SELECT code, MAX(date) AS md FROM seed_db.daily_quotes GROUP BY code) s
                LEFT JOIN (
                  SELECT code, MAX(date) AS md,
                         COALESCE(SUM(CASE WHEN volume != CAST(volume AS INTEGER)
                                           THEN 1 ELSE 0 END),0) AS demo
                    FROM daily_quotes GROUP BY code
                ) d ON d.code = s.code
               WHERE d.code IS NULL OR d.md < s.md OR d.demo > 0
            """
        )
        healed = con.execute("SELECT COUNT(*) FROM _heal").fetchone()[0]
        con.execute("DELETE FROM daily_quotes WHERE code IN (SELECT code FROM _heal)")
        con.execute(
            """
            INSERT INTO daily_quotes(code,date,open,high,low,close,volume,amount,turnover,pre_close)
              SELECT code,date,open,high,low,close,volume,amount,turnover,pre_close
                FROM seed_db.daily_quotes WHERE code IN (SELECT code FROM _heal)
            """
        )
        con.execute("DROP TABLE _heal")
        con.commit()          # 必须先提交, 事务未结束前 DETACH 会报 "database is locked"
        con.execute("DETACH DATABASE seed_db")
        print(f"SEED healed {healed} codes into app.db")
    except Exception as e:  # pragma: no cover - 自愈失败不应阻断启动
        print("SEED merge error(忽略):", e)
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        if con is not None:
            try:
                con.execute("DETACH DATABASE seed_db")
            except Exception:
                pass
            con.close()


if __name__ == "__main__":
    main()
