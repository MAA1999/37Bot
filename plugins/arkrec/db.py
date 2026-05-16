"""arkrec 数据库 —— 记录、关卡、规范化表、订阅"""

import json
import re
import sqlite3
from pathlib import Path

from ncatbot.utils import get_log

logger = get_log("ArkRec")


def _team_member_name(member) -> str:
    if isinstance(member, dict):
        return member.get("name", "")
    if isinstance(member, str):
        return member
    return ""


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
                    date_created TEXT,
                    grp TEXT,
                    synced_at TEXT DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_records_operation ON records(operation);
                CREATE INDEX IF NOT EXISTS idx_records_date ON records(date_published DESC);
                CREATE INDEX IF NOT EXISTS idx_records_synced ON records(synced_at DESC);

                -- 规范化分类表
                CREATE TABLE IF NOT EXISTS record_categories (
                    record_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    PRIMARY KEY (record_id, category)
                );
                CREATE INDEX IF NOT EXISTS idx_rc_category ON record_categories(category);

                -- 规范化干员表
                CREATE TABLE IF NOT EXISTS record_operators (
                    record_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    PRIMARY KEY (record_id, name)
                );
                CREATE INDEX IF NOT EXISTS idx_ro_name ON record_operators(name);

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
            self._migrate_normalized(c)

    def _migrate_normalized(self, c: sqlite3.Connection):
        """如果规范化表为空但 records 表有数据，从 JSON 列回填"""
        row = c.execute("SELECT COUNT(*) FROM record_categories").fetchone()
        if row[0] > 0:
            return  # 已有数据，跳过
        row = c.execute("SELECT COUNT(*) FROM records").fetchone()
        if row[0] == 0:
            return  # 无记录，跳过
        logger.info("回填规范化表...")
        for r in c.execute("SELECT _id, category_json, team_json FROM records").fetchall():
            _id = r[0]
            for cat in json.loads(r[1] or "[]"):
                c.execute("INSERT OR IGNORE INTO record_categories VALUES (?,?)", (_id, cat))
            for t in json.loads(r[2] or "[]"):
                name = _team_member_name(t)
                if name:
                    c.execute("INSERT OR IGNORE INTO record_operators VALUES (?,?)", (_id, name))
        c.commit()
        logger.info("规范化表回填完成")

    # ====== records ======

    def insert_records(self, data: list[dict]) -> list[str]:
        """批量插入/更新记录，返回本次新增的 _id 列表。
        已存在的记录会更新 url、grp、team、category、remark1 等可能补填的字段。
        """
        if not data:
            return []

        all_ids = [e["_id"] for e in data]
        with self._connect() as c:
            placeholders = ",".join("?" for _ in all_ids)
            existing = set(r[0] for r in c.execute(
                f"SELECT _id FROM records WHERE _id IN ({placeholders})", all_ids
            ).fetchall())

            new_ids = []
            for entry in data:
                _id = entry["_id"]
                if _id not in existing:
                    c.execute("""
                        INSERT INTO records
                        (_id, story, episode, operation, cn_name, operationType,
                         raider, raiderLink, raiderImage, team_json, modules_json,
                         category_json, url, remark1, date_published, date_created, grp)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        _id,
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
                        entry.get("date_created", ""),
                        entry.get("group", ""),
                    ))
                    new_ids.append(_id)
                    existing.add(_id)  # 防同一批次内重复 _id 再次走 INSERT
                else:
                    # 更新可能后补的字段
                    c.execute("""
                        UPDATE records SET
                            team_json=?,
                            modules_json=?,
                            category_json=?,
                            url=?,
                            remark1=?,
                            grp=?,
                            synced_at=datetime('now')
                        WHERE _id=?
                    """, (
                        json.dumps(entry.get("team", []), ensure_ascii=False),
                        json.dumps(entry.get("modules", {}), ensure_ascii=False),
                        json.dumps(entry.get("category", []), ensure_ascii=False),
                        entry.get("url", ""),
                        entry.get("remark1", ""),
                        entry.get("group", ""),
                        _id,
                    ))

                # 刷新规范化表
                c.execute("DELETE FROM record_categories WHERE record_id=?", (_id,))
                for cat in entry.get("category", []):
                    c.execute("INSERT OR IGNORE INTO record_categories VALUES (?,?)", (_id, cat))
                c.execute("DELETE FROM record_operators WHERE record_id=?", (_id,))
                for t in entry.get("team", []):
                    name = _team_member_name(t)
                    if name:
                        c.execute("INSERT OR IGNORE INTO record_operators VALUES (?,?)", (_id, name))
            c.commit()
        return new_ids

    def query_records(self, operation: str = "", category: str = "",
                      operator: str = "", mode: str = "", grp: str = "",
                      limit: int = 20, offset: int = 0) -> list[dict]:
        """灵活查询记录。分类和干员通过规范化表精确匹配。"""
        if category or operator:
            # 使用规范化表精确匹配
            sql = "SELECT DISTINCT r.* FROM records r"
            params: list = []
            if category:
                sql += " JOIN record_categories rc ON r._id = rc.record_id AND rc.category = ?"
                params.append(category)
            if operator:
                sql += " JOIN record_operators ro ON r._id = ro.record_id AND ro.name = ?"
                params.append(operator)
            sql += " WHERE 1=1"
            if operation:
                sql += " AND r.operation = ?"
                params.append(operation)
            if mode:
                sql += " AND r.operationType = ?"
                params.append(mode)
            if grp:
                sql += " AND r.grp = ?"
                params.append(grp)
            sql += " ORDER BY r._id DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        else:
            sql = "SELECT * FROM records WHERE 1=1"
            params = []
            if operation:
                sql += " AND operation = ?"
                params.append(operation)
            if mode:
                sql += " AND operationType = ?"
                params.append(mode)
            if grp:
                sql += " AND grp = ?"
                params.append(grp)
            sql += " ORDER BY _id DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        with self._connect() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def get_records_by_ids(self, ids: list[str]) -> list[dict]:
        """根据 _id 列表批量查询记录"""
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as c:
            return [dict(r) for r in c.execute(
                f"SELECT * FROM records WHERE _id IN ({placeholders})", ids
            ).fetchall()]

    def query_latest(self, limit: int = 20) -> list[dict]:
        with self._connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM records ORDER BY _id DESC LIMIT ?", (limit,)
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
