"""arkrec wiki API 客户端"""

import asyncio
import httpx

from ncatbot.utils import get_log

logger = get_log("ArkRec")

WIKI_BASE = "https://wiki.arkrec.com/v1"
MAX_CONCURRENT = 5


async def fetch_menu(client: httpx.AsyncClient) -> list[dict]:
    """获取全关卡树，摊平返回所有关卡节点"""
    resp = await client.get(f"{WIKI_BASE}/api/menu")
    resp.raise_for_status()
    menu = resp.json()

    ops = []

    def walk(node):
        if "operation" in node:
            ops.append(node)
        for c in node.get("childNodes", []):
            walk(c)

    walk(menu)
    logger.info(f"menu: {len(ops)} 个关卡")
    return ops


async def fetch_records_for_operation(
    client: httpx.AsyncClient, operation: str, cn_name: str
) -> list[dict]:
    """获取单个关卡的记录"""
    resp = await client.post(
        f"{WIKI_BASE}/api/records",
        json={"operation": operation, "cn_name": cn_name},
    )
    if resp.status_code != 200:
        logger.warning(f"records {operation} HTTP {resp.status_code}")
        return []
    return resp.json()


async def fetch_latest_records(
    client: httpx.AsyncClient, skip: int = 0
) -> list[dict]:
    """获取最新记录（增量）"""
    resp = await client.post(
        f"{WIKI_BASE}/record/latest-records",
        json={"skip": skip},
    )
    if resp.status_code != 200:
        logger.warning(f"latest-records skip={skip} HTTP {resp.status_code}")
        return []
    data = resp.json()
    logger.debug(f"latest-records skip={skip}: {len(data)} 条")
    return data


async def fetch_operators(client: httpx.AsyncClient) -> list[dict]:
    """获取全干员列表"""
    resp = await client.get(f"{WIKI_BASE}/api/operators")
    resp.raise_for_status()
    return resp.json()


async def fetch_categories(client: httpx.AsyncClient) -> dict:
    """获取全部分类"""
    resp = await client.get(f"{WIKI_BASE}/api/categories")
    resp.raise_for_status()
    return resp.json()


async def full_sync(db, client: httpx.AsyncClient, sem: asyncio.Semaphore):
    """全量同步：拉 menu → 每关拉 records → 入库。
    已存在 _id 的记录跳过，只插新数据。
    """
    ops = await fetch_menu(client)
    # 先存关卡元数据
    from plugins.arkrec.db import ArkRecDB
    if isinstance(db, ArkRecDB):
        db.upsert_operations(ops)

    total = 0

    async def sync_one(op):
        nonlocal total
        async with sem:
            records = await fetch_records_for_operation(
                client, op["operation"], op["cn_name"]
            )
            if records:
                n = db.insert_records(records)
                total += n
                if n > 0:
                    logger.debug(f"  {op['operation']}: +{n}")

    # 分批并发
    batch_size = 50
    for i in range(0, len(ops), batch_size):
        batch = ops[i : i + batch_size]
        await asyncio.gather(*[sync_one(op) for op in batch])
        logger.info(f"全量同步进度: {min(i + batch_size, len(ops))}/{len(ops)}, 新增 {total}")

    logger.info(f"全量同步完成: {len(ops)} 关, 新增 {total} 条记录")
    return total


async def incremental_sync(db, client: httpx.AsyncClient):
    """增量同步：拉 latest-records 最近几页"""
    total = 0
    for skip in (0, 2, 22, 42):
        records = await fetch_latest_records(client, skip)
        if not records:
            break
        n = db.insert_records(records)
        total += n
    if total:
        logger.info(f"增量同步: +{total} 条")
    return total


