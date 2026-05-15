"""全量同步脚本 —— 删旧库 → 拉全量

用法: uv run python -m plugins.arkrec.sync_now
前提: 已通过 /arkrec_config 配置账号
"""
import asyncio
import json
from pathlib import Path

from plugins.arkrec.auth import ArkRecAuth
from plugins.arkrec.db import ArkRecDB
from plugins.arkrec.api import full_sync, sync_exclusive


async def main():
    data_dir = Path("data/ArkRecPlugin")
    data_dir.mkdir(parents=True, exist_ok=True)

    cfg_path = data_dir / "config.json"
    if not cfg_path.exists():
        print("请先在群里私聊机器人执行 /arkrec_config <email> <password> 配置账号")
        return

    cfg = json.loads(cfg_path.read_text("utf-8"))
    email = cfg.get("email", "")
    password = cfg.get("password", "")
    if not email or not password:
        print("账号未配置，请先 /arkrec_config")
        return

    db_path = data_dir / "arkrec.db"
    if db_path.exists():
        db_path.unlink()
        print(f"已删除旧数据库: {db_path}")

    db = ArkRecDB(db_path)
    print("数据库初始化完成")

    auth = ArkRecAuth(data_dir, email, password)
    client = await auth.get_client()

    sem = asyncio.Semaphore(5)

    print("开始全量同步...")
    total = await full_sync(db, client, sem)
    print(f"全量同步完成: {total} 条记录")

    print("同步专属记录...")
    try:
        ex_total = await sync_exclusive(db, client)
        print(f"专属记录: {ex_total} 条")
    except Exception as e:
        import traceback
        print(f"专属记录同步失败: {type(e).__name__}: {e}")
        traceback.print_exc()

    count = db.get_record_count()
    print(f"数据库总记录: {count} 条")

    await auth.close()


if __name__ == "__main__":
    asyncio.run(main())
