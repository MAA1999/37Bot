"""全量同步脚本 —— 删旧库 → 拉全量

用法: uv run python -m plugins.arkrec.sync_now
公开数据无需登录，使用游客模式。
"""
import asyncio
from pathlib import Path

import httpx

from plugins.arkrec.db import ArkRecDB
from plugins.arkrec.api import full_sync

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)


async def main():
    data_dir = Path("data/ArkRecPlugin")
    data_dir.mkdir(parents=True, exist_ok=True)

    db_path = data_dir / "arkrec.db"
    if db_path.exists():
        db_path.unlink()
        print(f"已删除旧数据库: {db_path}")

    db = ArkRecDB(db_path)
    print("数据库初始化完成")

    async with httpx.AsyncClient(
        headers={"User-Agent": CHROME_UA},
        timeout=30,
    ) as client:
        sem = asyncio.Semaphore(5)

        print("开始全量同步...")
        total = await full_sync(db, client, sem)
        print(f"全量同步完成: {total} 条记录")

        count = db.get_record_count()
        print(f"数据库总记录: {count} 条")


if __name__ == "__main__":
    asyncio.run(main())
