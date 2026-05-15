"""arkrec 数据库 —— 记录、关卡、专属记录、订阅"""

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from ncatbot.utils import get_log

logger = get_log("ArkRec")


class ArkRecDB:
    def __init__(self, db_path: Path):
        self.db_path = str(db_path)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self):
        with self._connect() as c:
            c.executescript("""
                -- 少人通关记录
                CREATE TABLE IF NOT EXISTS records (
                    _id TEXT PRIMARY KEY,
                    story TEXT,
                    episode TEXT,
                    operation TEXT,
                    cn_name TEXT,
                    operationType TEXT,
                    raider TEXT,
                    raiderLink TEXT,
                    raiderImage TEXT,
                    team_json TEXT,
                    modules_json TEXT,
                    category_json TEXT,
                    url TEXT,
                    remark1 TEXT,
                    date_published TEXT,
                    synced_at TEXT DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_records_operation ON records(operation);
                CREATE INDEX IF NOT EXISTS idx_records_date ON records(date_published DESC);
                CREATE INDEX IF NOT EXISTS idx_records_synced ON records(synced_at DESC);

                -- 关卡元数据 (来自 /api/menu)
                CREATE TABLE IF NOT EXISTS operations (
                    operation TEXT PRIMARY KEY,
                    cn_name TEXT,
                    episode TEXT,
                    story TEXT,
                    preview TEXT,
                    challenge TEXT,
                    hasChallenge INTEGER DEFAULT 0,
                    stageId TEXT,
                    zone TEXT
                );

                -- 群订阅
                CREATE TABLE IF NOT EXISTS subscriptions (
                    group_id TEXT,
                    filter_type TEXT,
                    filter_value TEXT,
                    PRIMARY KEY (group_id, filter_type, filter_value)
                );
            """)
            c.commit()

    # ====== records ======

    def insert_records(self, data: list[dict]) -> int:
        """批量插入记录，返回新增数。_id 重复则跳过。"""
        new = 0
        with self._connect() as c:
            for entry in data:
                try:
                    c.execute("""
                        INSERT OR IGNORE INTO records
                        (_id, story, episode, operation, cn_name, operationType,
                         raider, raiderLink, raiderImage, team_json, modules_json,
                         category_json, url, remark1, date_published)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        entry["_id"],
                        entry.get("story", ""),
                        entry.get("episode", ""),
                        entry.get("operation", ""),
                        entry.get("cn_name", ""),
                        entry.get("operationType", ""),
                        entry.get("raider", ""),
                        entry.get("raiderLink", ""),
                        entry.get("raiderImage", ""),
                        json.dumps(entry.get("team", []), ensure_ascii=False),
                        json.dumps(entry.get("modules", {}), ensure_ascii=False),
                        json.dumps(entry.get("category", []), ensure_ascii=False),
                        entry.get("url", ""),
                        entry.get("remark1", ""),
                        entry.get("date_published", ""),
                    ))
                    if c.rowcount > 0:
                        new += 1
                except Exception as e:
                    logger.debug(f"insert_record skip: {e}")
            c.commit()
        return new

    def query_records(self, operation: str = "", category: str = "",
                      operator: str = "", mode: str = "",
                      limit: int = 20, offset: int = 0) -> list[dict]:
        """灵活查询记录"""
        sql = "SELECT * FROM records WHERE 1=1"
        params = []
        if operation:
            sql += " AND operation = ?"
            params.append(operation)
        if category:
            sql += " AND category_json LIKE ?"
            params.append(f"%{category}%")
        if operator:
            sql += " AND team_json LIKE ?"
            params.append(f"%{operator}%")
        if mode:
            sql += " AND operationType = ?"
            params.append(mode)
        sql += " ORDER BY date_published DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._connect() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def query_latest(self, limit: int = 20) -> list[dict]:
        with self._connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM records ORDER BY date_published DESC LIMIT ?", (limit,)
            ).fetchall()]

    def get_record_count(self) -> int:
        with self._connect() as c:
            return c.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    # ====== operations ======

    def upsert_operations(self, ops: list[dict]):
        """批量写入关卡元数据"""
        with self._connect() as c:
            for op in ops:
                c.execute("""
                    INSERT OR REPLACE INTO operations
                    (operation, cn_name, episode, story, preview, challenge, hasChallenge, stageId, zone)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, (
                    op.get("operation", ""),
                    op.get("cn_name", ""),
                    op.get("episode", ""),
                    op.get("story", ""),
                    op.get("preview", ""),
                    op.get("challenge", ""),
                    1 if op.get("hasChallenge") else 0,
                    op.get("stageId", ""),
                    op.get("zone", ""),
                ))
            c.commit()

    def query_operations(self, keyword: str = "", limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM operations WHERE 1=1"
        params = []
        if keyword:
            sql += " AND (operation LIKE ? OR cn_name LIKE ?)"
            kw = f"%{keyword}%"
            params.extend([kw, kw])
        sql += " LIMIT ?"
        params.append(limit)
        with self._connect() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def resolve_operation(self, normalized: str) -> list[str]:
        """用归一化字符串匹配关卡号，返回所有匹配的标准名列表，按前缀短的优先"""
        with self._connect() as c:
            rows = c.execute("SELECT operation FROM operations").fetchall()
        matches = []
        for (op,) in rows:
            if re.sub(r"[^A-Za-z0-9]", "", op).upper() == normalized:
                matches.append(op)
        matches.sort(key=lambda o: len(o.split("-")[0]))
        return matches

    # ====== subscriptions ======

    def add_subscription(self, group_id: str, filter_type: str, filter_value: str):
        with self._connect() as c:
            c.execute(
                "INSERT OR IGNORE INTO subscriptions VALUES (?,?,?)",
                (group_id, filter_type, filter_value)
            )
            c.commit()

    def remove_subscription(self, group_id: str, filter_type: str, filter_value: str):
        with self._connect() as c:
            c.execute(
                "DELETE FROM subscriptions WHERE group_id=? AND filter_type=? AND filter_value=?",
                (group_id, filter_type, filter_value)
            )
            c.commit()

    def get_subscriptions(self, group_id: str) -> list[dict]:
        with self._connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM subscriptions WHERE group_id=?", (group_id,)
            ).fetchall()]

    def get_all_subscription_groups(self) -> list[str]:
        with self._connect() as c:
            return [r[0] for r in c.execute(
                "SELECT DISTINCT group_id FROM subscriptions"
            ).fetchall()]

    def get_subscribed_filters_for_group(self, group_id: str) -> dict[str, list[str]]:
        """返回 {filter_type: [values]}"""
        result: dict[str, list[str]] = {}
        for sub in self.get_subscriptions(group_id):
            result.setdefault(sub["filter_type"], []).append(sub["filter_value"])
        return result
