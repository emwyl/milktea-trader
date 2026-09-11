"""SQLite 引擎与会话。零运维，后期迁云只需替换 SQLITE_URL。"""
from __future__ import annotations
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.config import SQLITE_URL

engine = create_engine(SQLITE_URL, connect_args={"check_same_thread": False, "timeout": 30}, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_migrations(engine):
    """SQLite 轻量迁移：为旧库补充 user_id/role 等列，并把无主数据归到 admin 名下。"""
    from sqlalchemy import text
    from app.config import DEFAULT_USERNAME

    def _has_table(conn, name):
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"), {"n": name}).fetchall()
        return bool(rows)

    def _has_col(conn, table, col):
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        return any(r[1] == col for r in rows)

    with engine.begin() as conn:
        cols_to_add = [
            ("users", "role", "VARCHAR(16) DEFAULT 'user'"),
            ("users", "is_guest", "BOOLEAN DEFAULT 0"),
            ("users", "is_active", "BOOLEAN DEFAULT 1"),
            ("users", "last_ip", "VARCHAR(48)"),
            ("screens", "user_id", "INTEGER"),
            ("tracked_pool", "user_id", "INTEGER"),
            ("position_rules", "user_id", "INTEGER"),
            ("signals", "user_id", "INTEGER"),
            ("user_profile", "user_id", "INTEGER"),
            ("notify_config", "user_id", "INTEGER"),
            ("notify_log", "user_id", "INTEGER"),
            ("access_logs", "user_id", "INTEGER"),
        ]
        for table, col, dtype in cols_to_add:
            if _has_table(conn, table) and not _has_col(conn, table, col):
                try:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {dtype}"))
                except Exception:
                    pass

        # 已有 admin 的 role 修正为 admin（默认游客/新注册仍为 user）
        try:
            conn.execute(text("UPDATE users SET role='admin' WHERE username=:u"), {"u": DEFAULT_USERNAME})
        except Exception:
            pass

        # 把无主数据归到第一个 admin（通常是默认 admin）
        admin_row = conn.execute(text("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1")).fetchone()
        admin_id = admin_row[0] if admin_row else None
        if admin_id:
            for table in ["screens", "tracked_pool", "position_rules", "signals",
                          "user_profile", "notify_config", "notify_log", "stock_tconfig"]:
                if _has_col(conn, table, "user_id"):
                    try:
                        conn.execute(text(f"UPDATE {table} SET user_id=:uid WHERE user_id IS NULL"), {"uid": admin_id})
                    except Exception:
                        pass

        # access_logs 新增 user_id 后，按 username 回填对应 users.id（游客/未匹配 username 保持 NULL）
        if _has_col(conn, "access_logs", "user_id"):
            try:
                conn.execute(text("""
                    UPDATE access_logs
                    SET user_id = (SELECT id FROM users WHERE users.username = access_logs.username LIMIT 1)
                    WHERE user_id IS NULL AND username IS NOT NULL
                """))
            except Exception:
                pass

        # stock_tconfig 旧表主键是 code，无法支持多用户；需要重建为 (id PK + user_id+code 唯一)
        if _has_table(conn, "stock_tconfig"):
            info = conn.execute(text("PRAGMA table_info(stock_tconfig)")).fetchall()
            col_names = {r[1] for r in info}
            if "id" not in col_names:
                conn.execute(text("ALTER TABLE stock_tconfig RENAME TO stock_tconfig_legacy"))
                conn.execute(text("""
                    CREATE TABLE stock_tconfig (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        code VARCHAR(16),
                        custom_support FLOAT,
                        custom_pressure FLOAT,
                        risk_note TEXT DEFAULT '',
                        updated_at VARCHAR(32) DEFAULT '',
                        FOREIGN KEY(user_id) REFERENCES users(id)
                    )
                """))
                conn.execute(text("CREATE INDEX ix_stock_tconfig_user_id ON stock_tconfig(user_id)"))
                conn.execute(text("CREATE UNIQUE INDEX uq_stock_tconfig_user_code ON stock_tconfig(user_id, code)"))
                if admin_id:
                    conn.execute(text("""
                        INSERT INTO stock_tconfig (user_id, code, custom_support, custom_pressure, risk_note, updated_at)
                        SELECT :uid, code, custom_support, custom_pressure, risk_note, updated_at
                        FROM stock_tconfig_legacy
                    """), {"uid": admin_id})
                conn.execute(text("DROP TABLE stock_tconfig_legacy"))

        # #29-3 已于 2026-09-07 废弃：原逻辑「默认 admin 仍在用出厂密码就强制首登改密」会让用户反复被要求改密。
        # 现改为：仍在用旧出厂密码(admin123)、或被打了强制改密标记的 admin，
        # 统一重置为新出厂密码(baofu123)并清除标记，保证升级后可直接用 baofu123 登录。
        try:
            from app.config import DEFAULT_PASSWORD
            from app.security import hash_password, verify_password
            row = conn.execute(
                text("SELECT id, salt, password_hash FROM users WHERE username=:u"),
                {"u": DEFAULT_USERNAME},
            ).fetchone()
            if row and row[0]:
                need = False
                if row[1] and row[2] and verify_password("admin123", row[1], row[2]):
                    need = True  # 仍在用旧出厂密码
                mc = conn.execute(text("SELECT must_change_pw FROM users WHERE id=:i"), {"i": row[0]}).fetchone()
                if mc and mc[0]:
                    need = True  # 被打了强制改密标记
                if need:
                    h, s = hash_password(DEFAULT_PASSWORD)
                    conn.execute(
                        text("UPDATE users SET password_hash=:h, salt=:s, must_change_pw=0 WHERE id=:i"),
                        {"h": h, "s": s, "i": row[0]},
                    )
        except Exception:
            pass

        # #29-2：清理历史随机游客账号（游客_xxx），游客统一收敛到唯一系统账号 guest
        if _has_table(conn, "users") and _has_col(conn, "users", "is_guest"):
            try:
                legacy_ids = [r[0] for r in conn.execute(
                    text("SELECT id FROM users WHERE is_guest=1 AND username LIKE '游客_%'")
                ).fetchall()]
                if legacy_ids:
                    guest_tables = ["tracked_pool", "screens", "position_rules", "signals",
                                    "user_profile", "notify_config", "notify_log",
                                    "user_settings", "stock_tconfig", "access_logs"]
                    for uid in legacy_ids:
                        for t in guest_tables:
                            if _has_table(conn, t) and _has_col(conn, t, "user_id"):
                                try:
                                    conn.execute(text(f"DELETE FROM {t} WHERE user_id=:u"), {"u": uid})
                                except Exception:
                                    pass
                        try:
                            conn.execute(text("DELETE FROM users WHERE id=:u"), {"u": uid})
                        except Exception:
                            pass
            except Exception:
                pass

        # 可投池自定义标签表（pool_tags）：用户级标签名称 + 填充色
        if not _has_table(conn, "pool_tags"):
            conn.execute(text("""
                CREATE TABLE pool_tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    name VARCHAR(64) DEFAULT '',
                    color VARCHAR(16) DEFAULT '#3b82f6',
                    created_at VARCHAR(32) DEFAULT '',
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
            """))
            conn.execute(text("CREATE INDEX ix_pool_tags_user_id ON pool_tags(user_id)"))

        # 可投池与标签的多对多关联表
        if not _has_table(conn, "tracked_pool_tags"):
            conn.execute(text("""
                CREATE TABLE tracked_pool_tags (
                    pool_id INTEGER NOT NULL,
                    tag_id INTEGER NOT NULL,
                    user_id INTEGER,
                    PRIMARY KEY (pool_id, tag_id),
                    FOREIGN KEY(pool_id) REFERENCES tracked_pool(id),
                    FOREIGN KEY(tag_id) REFERENCES pool_tags(id),
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
            """))
            conn.execute(text("CREATE INDEX ix_tracked_pool_tags_tag_id ON tracked_pool_tags(tag_id)"))
            conn.execute(text("CREATE INDEX ix_tracked_pool_tags_user_id ON tracked_pool_tags(user_id)"))
        else:
            # v133: 给旧表补上 user_id 并回填，确保按账号清理关联不留孤儿记录
            if not _has_col(conn, "tracked_pool_tags", "user_id"):
                try:
                    conn.execute(text("ALTER TABLE tracked_pool_tags ADD COLUMN user_id INTEGER"))
                    conn.execute(text("CREATE INDEX ix_tracked_pool_tags_user_id ON tracked_pool_tags(user_id)"))
                except Exception:
                    pass
            if _has_col(conn, "tracked_pool_tags", "user_id"):
                try:
                    conn.execute(text("""
                        UPDATE tracked_pool_tags
                        SET user_id = (SELECT user_id FROM tracked_pool WHERE tracked_pool.id = tracked_pool_tags.pool_id)
                        WHERE user_id IS NULL
                    """))
                except Exception:
                    pass

        # 旧版 tracked_pool.tag_id 单标签字段迁移到多对多关联表
        if _has_col(conn, "tracked_pool", "tag_id") and _has_table(conn, "tracked_pool_tags"):
            try:
                conn.execute(text("""
                    INSERT OR IGNORE INTO tracked_pool_tags (pool_id, tag_id)
                    SELECT id, tag_id FROM tracked_pool WHERE tag_id IS NOT NULL
                """))
            except Exception:
                pass

        # v135：tracked_pool 加 day_view（日初主观判断）。允许为空字符串。
        if _has_col(conn, "tracked_pool", "id"):
            try:
                if not _has_col(conn, "tracked_pool", "day_view"):
                    conn.execute(text("ALTER TABLE tracked_pool ADD COLUMN day_view VARCHAR(16) DEFAULT ''"))
            except Exception:
                pass

        # v189：监控链路两列（详见 models.py TrackedPool 注释）。
        #   monitored 默认 1、source 默认 'pretrade' —— 存量行一并填上，保证老数据三页照常可见。
        if _has_col(conn, "tracked_pool", "id"):
            try:
                if not _has_col(conn, "tracked_pool", "monitored"):
                    conn.execute(text("ALTER TABLE tracked_pool ADD COLUMN monitored INTEGER DEFAULT 1"))
            except Exception:
                pass
            try:
                if not _has_col(conn, "tracked_pool", "source"):
                    conn.execute(text("ALTER TABLE tracked_pool ADD COLUMN source VARCHAR(16) DEFAULT 'pretrade'"))
            except Exception:
                pass
            # 兜底：历史行若因任何原因落在 NULL，统一补成「已监控 + 盘前来源」，避免数据从三页消失
            try:
                conn.execute(text("UPDATE tracked_pool SET monitored=1 WHERE monitored IS NULL"))
                conn.execute(text("UPDATE tracked_pool SET source='pretrade' WHERE source IS NULL OR source=''"))
            except Exception:
                pass

        # v189：日线表补主力资金两列（盘前预判要取 T-1 静态值，只能靠每日落库积累）。
        #   daily_quotes 行数很多，但 SQLite 的 ALTER ADD COLUMN 只改 schema、不重写数据，成本极低。
        if _has_col(conn, "daily_quotes", "id"):
            try:
                if not _has_col(conn, "daily_quotes", "main_net_pct"):
                    conn.execute(text("ALTER TABLE daily_quotes ADD COLUMN main_net_pct FLOAT"))
            except Exception:
                pass
            try:
                if not _has_col(conn, "daily_quotes", "main_signal"):
                    conn.execute(text("ALTER TABLE daily_quotes ADD COLUMN main_signal VARCHAR(64) DEFAULT ''"))
            except Exception:
                pass

        # v190：流通股本缓存表（换手率 = 成交量 ÷ 流通股本，日K接口不含换手率，只能自算）。
        if not _has_table(conn, "stock_float_shares"):
            try:
                conn.execute(text("""
                    CREATE TABLE stock_float_shares (
                        code VARCHAR(16) PRIMARY KEY,
                        shares FLOAT DEFAULT 0.0,
                        updated_at VARCHAR(32)
                    )
                """))
            except Exception:
                pass

        # v143：日初判断修改记录表（day_view_log）。
        #   每次修改都追加一条（不删旧），同一天允许多条；
        #   「当日最新」=同日 operated_at 最大；盯盘日志按交易日聚合，每交易日取最后一条作为复盘输入。
        #   v178：补 composite_score / reference_text / reference_metrics_json 三列——录入时刻的
        #   综合评分快照 + 预判参考信息文本 + 结构化指标 JSON，用于盯盘日志表格新增两列展示。
        if not _has_table(conn, "day_view_log"):
            try:
                conn.execute(text("""
                    CREATE TABLE day_view_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        code VARCHAR(16),
                        trade_date VARCHAR(10),
                        trend VARCHAR(4) DEFAULT '-',
                        target_price FLOAT,
                        target_note VARCHAR(40) DEFAULT '',
                        operator VARCHAR(64) DEFAULT '',
                        operator_id INTEGER,
                        operated_at VARCHAR(32) DEFAULT '',
                        composite_score FLOAT,
                        reference_text TEXT DEFAULT '',
                        reference_metrics_json TEXT DEFAULT '',
                        FOREIGN KEY(user_id) REFERENCES users(id)
                    )
                """))
                # 复合索引：按 (user, code, trade_date, operated_at) 高频查
                conn.execute(text("CREATE INDEX ix_day_view_log_user_code_date_oper ON day_view_log(user_id, code, trade_date, operated_at)"))
                conn.execute(text("CREATE INDEX ix_day_view_log_code ON day_view_log(code)"))
                conn.execute(text("CREATE INDEX ix_day_view_log_user_id ON day_view_log(user_id)"))
            except Exception:
                pass
        else:
            # v178：旧库 day_view_log 缺新三列,逐列补(已有则跳过)。
            for col, ddl in (
                ("composite_score", "ALTER TABLE day_view_log ADD COLUMN composite_score FLOAT"),
                ("reference_text", "ALTER TABLE day_view_log ADD COLUMN reference_text TEXT DEFAULT ''"),
                ("reference_metrics_json", "ALTER TABLE day_view_log ADD COLUMN reference_metrics_json TEXT DEFAULT ''"),
                # v185: 日初预判目标价位拆分为「目标买入 / 目标卖出」两端(旧 target_price 保留兼容)
                ("target_buy", "ALTER TABLE day_view_log ADD COLUMN target_buy FLOAT"),
                ("target_sell", "ALTER TABLE day_view_log ADD COLUMN target_sell FLOAT"),
            ):
                try:
                    if not _has_col(conn, "day_view_log", col):
                        conn.execute(text(ddl))
                except Exception:
                    pass

        # v146: 偏离原因复盘表(day_view_recap)。
        #   每 (user, code, trade_date) 一行,只存最新一份;双击单元格保存时覆盖更新 updated_at。
        #   便于后续按 (user, date_range) 聚合做月度预判-复盘准确率统计。
        if not _has_table(conn, "day_view_recap"):
            try:
                conn.execute(text("""
                    CREATE TABLE day_view_recap (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        code VARCHAR(16),
                        trade_date VARCHAR(10),
                        recap TEXT DEFAULT '',
                        operator VARCHAR(64) DEFAULT '',
                        operator_id INTEGER,
                        updated_at VARCHAR(32) DEFAULT '',
                        FOREIGN KEY(user_id) REFERENCES users(id)
                    )
                """))
                conn.execute(text("CREATE UNIQUE INDEX uq_day_view_recap_user_code_date ON day_view_recap(user_id, code, trade_date)"))
                conn.execute(text("CREATE INDEX ix_day_view_recap_code ON day_view_recap(code)"))
                conn.execute(text("CREATE INDEX ix_day_view_recap_user_date ON day_view_recap(user_id, trade_date)"))
            except Exception:
                pass

        # v173：规则级风控提示。每条加减仓规则可自带一段"风控提示"，
        #   命中该规则的信号在「最新信号」表里直接展示这段文字（不再只用系统自动生成的通用建议）。
        if _has_table(conn, "position_rules") and not _has_col(conn, "position_rules", "risk_notice"):
            try:
                conn.execute(text("ALTER TABLE position_rules ADD COLUMN risk_notice TEXT DEFAULT ''"))
            except Exception:
                pass
