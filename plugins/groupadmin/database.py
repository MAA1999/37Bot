"""成员记录数据库"""

import sqlite3
from pathlib import Path
from typing import Optional, List
from dataclasses import dataclass


@dataclass
class MemberRecord:
    """成员记录"""
    user_id: str
    group_id: str
    join_time: Optional[int] = None
    leave_time: Optional[int] = None
    join_answer: Optional[str] = None
    join_type: Optional[str] = None
    leave_type: Optional[str] = None


class MemberDB:
    """成员记录数据库"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """初始化数据库"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    join_time INTEGER,
                    leave_time INTEGER,
                    join_answer TEXT,
                    join_type TEXT,
                    leave_type TEXT,
                    UNIQUE(user_id, group_id, join_time)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_group_user
                ON members(group_id, user_id)
            """)
            conn.commit()

    def add_join_record(
        self,
        user_id: str,
        group_id: str,
        join_time: int,
        join_answer: str = None,
        join_type: str = None,
    ):
        """添加入群记录"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO members
                (user_id, group_id, join_time, join_answer, join_type)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, group_id, join_time, join_answer, join_type),
            )
            conn.commit()

    def update_leave_record(
        self,
        user_id: str,
        group_id: str,
        leave_time: int,
        leave_type: str = None,
    ):
        """更新退群记录（更新最近一条入群记录）"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE members SET leave_time = ?, leave_type = ?
                WHERE id = (
                    SELECT id FROM members
                    WHERE user_id = ? AND group_id = ? AND leave_time IS NULL
                    ORDER BY join_time DESC LIMIT 1
                )
                """,
                (leave_time, leave_type, user_id, group_id),
            )
            conn.commit()

    def get_member_records(
        self, group_id: str, user_id: str = None
    ) -> List[MemberRecord]:
        """查询成员记录"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if user_id:
                rows = conn.execute(
                    "SELECT * FROM members WHERE group_id = ? AND user_id = ?",
                    (group_id, user_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM members WHERE group_id = ?",
                    (group_id,),
                ).fetchall()
            return [
                MemberRecord(
                    user_id=r["user_id"],
                    group_id=r["group_id"],
                    join_time=r["join_time"],
                    leave_time=r["leave_time"],
                    join_answer=r["join_answer"],
                    join_type=r["join_type"],
                    leave_type=r["leave_type"],
                )
                for r in rows
            ]
