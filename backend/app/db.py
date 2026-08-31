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
                          "user_profile", "notify_config", "notify_log"]:
                if _has_col(conn, table, "user_id"):
                    try:
                        conn.execute(text(f"UPDATE {table} SET user_id=:uid WHERE user_id IS NULL"), {"uid": admin_id})
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
                    PRIMARY KEY (pool_id, tag_id),
                    FOREIGN KEY(pool_id) REFERENCES tracked_pool(id),
                    FOREIGN KEY(tag_id) REFERENCES pool_tags(id)
                )
            """))
            conn.execute(text("CREATE INDEX ix_tracked_pool_tags_tag_id ON tracked_pool_tags(tag_id)"))

        # 旧版 tracked_pool.tag_id 单标签字段迁移到多对多关联表
        if _has_col(conn, "tracked_pool", "tag_id") and _has_table(conn, "tracked_pool_tags"):
            try:
                conn.execute(text("""
                    INSERT OR IGNORE INTO tracked_pool_tags (pool_id, tag_id)
                    SELECT id, tag_id FROM tracked_pool WHERE tag_id IS NOT NULL
                """))
            except Exception:
                pass
