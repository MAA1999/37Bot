"""Todo 插件 - 群待办功能"""

import json
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

from ncatbot.plugin_system import NcatBotPlugin, command_registry, param
from ncatbot.core.event import GroupMessageEvent
from ncatbot.core.event.message_segment import Reply
from ncatbot.core.helper import ForwardConstructor
from ncatbot.utils import get_log

logger = get_log("Todo")


@dataclass
class TodoItem:
    """待办项"""
    id: int
    content: str  # 文本内容
    message_id: Optional[str] = None  # 原消息ID（如果是回复添加的）
    user_id: str = ""  # 添加者
    create_time: int = 0


class TodoPlugin(NcatBotPlugin):
    name = "TodoPlugin"
    version = "1.0.0"
    author = "Windsland52"
    dependencies = {}

    async def on_load(self):
        """插件加载"""
        self.data_path = self.workspace / "todos.json"
        self.todos = self._load_todos()  # {group_id: [TodoItem]}

    def _load_todos(self) -> dict:
        if self.data_path.exists():
            try:
                data = json.loads(self.data_path.read_text(encoding="utf-8"))
                result = {}
                for gid, items in data.items():
                    result[gid] = [TodoItem(**item) for item in items]
                return result
            except Exception:
                pass
        return {}

    def _save_todos(self):
        data = {
            gid: [asdict(item) for item in items]
            for gid, items in self.todos.items()
        }
        self.data_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _get_next_id(self, group_id: str) -> int:
        items = self.todos.get(group_id, [])
        if not items:
            return 1
        return max(item.id for item in items) + 1

    @command_registry.command("todo_add", description="添加待办(可回复消息)")
    @param(name="content", default="", help="待办内容")
    async def cmd_add(self, event: GroupMessageEvent, content: str = ""):
        """添加待办"""
        import time
        group_id = str(event.group_id)

        # 检查是否回复了消息
        reply_msg_id = None
        if hasattr(event, 'message') and event.message:
            for seg in event.message:
                if isinstance(seg, Reply):
                    reply_msg_id = seg.id
                    break

        if not content and not reply_msg_id:
            await event.reply("请输入待办内容或回复一条消息")
            return

        if group_id not in self.todos:
            self.todos[group_id] = []

        item = TodoItem(
            id=self._get_next_id(group_id),
            content=content,
            message_id=reply_msg_id,
            user_id=str(event.user_id),
            create_time=int(time.time()),
        )
        self.todos[group_id].append(item)
        self._save_todos()
        await event.reply(f"已添加待办 #{item.id}")

    @command_registry.command("todo_list", description="查看待办列表")
    async def cmd_list(self, event: GroupMessageEvent):
        """查看待办列表（转发消息展示）"""
        group_id = str(event.group_id)
        items = self.todos.get(group_id, [])

        if not items:
            await event.reply("暂无待办")
            return

        # 构建转发消息
        info = await self.api.get_login_info()
        fc = ForwardConstructor(info.user_id, info.nickname)

        for item in items:
            if item.message_id:
                # 通过消息ID添加
                try:
                    fc.attach_message_id(item.message_id)
                except Exception:
                    fc.attach_text(f"#{item.id} [消息已失效]")
            else:
                fc.attach_text(f"#{item.id} {item.content}")

        await self.api.post_group_forward_msg(
            event.group_id, fc.to_forward()
        )

    @command_registry.command("todo_done", description="完成待办")
    async def cmd_done(self, event: GroupMessageEvent, id: int):
        """完成待办"""
        group_id = str(event.group_id)
        items = self.todos.get(group_id, [])

        for i, item in enumerate(items):
            if item.id == id:
                items.pop(i)
                self._save_todos()
                await event.reply(f"已完成待办 #{id}")
                return

        await event.reply(f"未找到待办 #{id}")


__all__ = ["TodoPlugin"]
