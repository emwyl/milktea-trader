#!/usr/bin/env python
"""把本地 db 的精简版同步到 publish/backend/data/app.db，部署时 sandbox 启动会用这份数据。

为什么不直接拷整个 db?
  - data/app.db ~86MB，daily_quotes 占 48MB（行情缓存，可由 warmup 异步重建）
  - 部署包过大，平台可能拒收；且 daily_quotes 隔天就过期，没必要打包
  - publish/backend/data/ 当前是 v119 时代的旧 40 只股 db，每次发版后用户线上数据被冲掉

精简后 publish/backend/data/app.db ≈ 350KB，涵盖：
  - users（含 admin 密码）
  - stocks 全 A 股 5549 只（300377 等都在）
  - tracked_pool + pool_tags + tracked_pool_tags（用户选股池+标签）
  - position_rules / screens / signals / scheme_types / user_profile / user_settings
  - notify_config / notify_log / app_settings

用法:
  python tools/sync_publish_db.py                # 默认从 data/app.db 同步到 publish/backend/data/app.db
  python tools/sync_publish_db.py --src X.db --dst Y.db
  python tools/sync_publish_db.py --dry-run       # 只打印统计，不写文件
"""
import sqlite3, os, shutil, argparse, sys

KEEP_TABLES = [
    'users', 'stocks', 'screens', 'tracked_pool', 'position_rules',
    'signals', 'scheme_types', 'user_profile', 'notify_config',
    'notify_log', 'app_settings', 'screen_results', 'user_settings',
    'stock_tconfig', 'pool_tags', 'tracked_pool_tags',
]
# daily_quotes (48MB 行情缓存) + access_logs (访问日志) 不打包


def sync(src_path: str, dst_path: str, dry_run: bool = False) -> dict:
    src = sqlite3.connect(src_path)
    try:
        if dry_run:
            print(f'-- 源: {src_path}')
            print(f'-- 目标: {dst_path}')
        cur = src.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        tables = {r[0] for r in cur.fetchall()}
        keep = [t for t in KEEP_TABLES if t in tables]
        skipped = [t for t in KEEP_TABLES if t not in tables]
        if not dry_run:
            # 备份 dst 旧版
            if os.path.exists(dst_path):
                bak = dst_path + '.bak_pre_sync'
                if not os.path.exists(bak):
                    shutil.copy2(dst_path, bak)
            # 清掉 journal 等附属文件
            for ext in ('', '-journal', '-wal', '-shm'):
                p = dst_path + ext
                if os.path.exists(p):
                    os.remove(p)
        dst = sqlite3.connect(dst_path)
        try:
            counts = {}
            cur2 = src.cursor()
            cur2.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            for name, sql in cur2.fetchall():
                if name not in keep:
                    continue
                if not dry_run:
                    dst.execute(sql)
                cur3 = src.cursor()
                cur3.execute(f'SELECT * FROM {name}')
                rows = cur3.fetchall()
                if rows and not dry_run:
                    ncols = len(rows[0])
                    ph = ','.join(['?'] * ncols)
                    dst.executemany(f'INSERT INTO {name} VALUES ({ph})', rows)
                counts[name] = len(rows)
            if not dry_run:
                dst.commit()
                dst.execute('VACUUM')
        finally:
            dst.close()
    finally:
        src.close()
    sz = os.path.getsize(dst_path) if not dry_run else 0
    return {'counts': counts, 'skipped': skipped, 'size': sz}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default=r'D:\腾讯小龙虾\milktea-trader\data\app.db',
                    help='源 db 路径（默认本地主库）')
    ap.add_argument('--dst', default=r'D:\腾讯小龙虾\milktea-trader\publish\backend\data\app.db',
                    help='目标 db 路径（默认 publish 包内）')
    ap.add_argument('--dry-run', action='store_true', help='只打印统计，不写文件')
    args = ap.parse_args()

    if not os.path.exists(args.src):
        print(f'❌ 源 db 不存在: {args.src}', file=sys.stderr)
        sys.exit(1)

    r = sync(args.src, args.dst, dry_run=args.dry_run)
    print(f'=== 同步完成 ===' if not args.dry_run else '=== 预览 ===')
    print(f'目标: {args.dst}')
    if r['size']:
        print(f'大小: {r["size"]/1024:.1f} KB ({r["size"]/1024/1024:.2f} MB)')
    for t, n in r['counts'].items():
        print(f'  {t:25s}: {n:>7} 行')
    if r['skipped']:
        print(f'  (跳过不存在的表): {", ".join(r["skipped"])}')


if __name__ == '__main__':
    main()