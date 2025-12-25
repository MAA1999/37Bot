"""群管插件 - 处理加群请求和成员统计"""

import re
import json
from pathlib import Path
from dataclasses import asdict

from ncatbot.plugin_system import (
    NcatBotPlugin,
    command_registry,
    param,
    on_group_request,
    on_group_increase,
    on_notice,
)
from ncatbot.core.event import (
    GroupMessageEvent,
    BaseMessageEvent,
    RequestEvent,
    NoticeEvent,
)
from ncatbot.utils import get_log

from .config import GroupRule, GroupAdminConfig
from .database import MemberDB

logger = get_log("GroupAdmin")


class GroupAdminPlugin(NcatBotPlugin):
    name = "GroupAdminPlugin"
    version = "1.0.0"
    author = "Windsland52"
    dependencies = {}

    async def on_load(self):
        """插件加载"""
        self.config_path = self.workspace / "config.json"
        self.db = MemberDB(self.workspace / "members.db")
        self.config = self._load_config()
        # 缓存待处理的加群请求 {flag: (group_id, user_id, comment)}
        self.pending_requests = {}

    # ========== 配置管理 ==========

    def _load_config(self) -> GroupAdminConfig:
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                rules = [GroupRule(**r) for r in data.get("rules", [])]
                return GroupAdminConfig(rules=rules)
            except Exception:
                pass
        return GroupAdminConfig()

    def _save_config(self):
        data = {"rules": [asdict(r) for r in self.config.rules]}
        self.config_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _get_rule(self, group_id: str) -> GroupRule:
        for rule in self.config.rules:
            if rule.group_id == group_id:
                return rule
        return None

    def _get_or_create_rule(self, group_id: str) -> GroupRule:
        rule = self._get_rule(group_id)
        if rule is None:
            rule = GroupRule(group_id=group_id, enabled=False)
            self.config.rules.append(rule)
        return rule

    # ========== 事件处理 ==========

    @on_group_request
    async def handle_group_request(self, event: RequestEvent):
        """处理加群请求"""
        if not event.is_group_request():
            return

        group_id = event.group_id
        rule = self._get_rule(group_id)

        if rule is None or not rule.enabled:
            return

        comment = event.comment or ""
        user_id = event.user_id

        # 缓存请求信息
        self.pending_requests[event.flag] = (group_id, user_id, comment)

        # 检查回答是否匹配
        if rule.pattern:
            if re.search(rule.pattern, comment, re.IGNORECASE):
                await event.approve(True)
                logger.info(f"自动通过: group={group_id}, user={user_id}")
            elif rule.auto_reject:
                await event.approve(False, reason=rule.reject_reason)
                logger.info(f"自动拒绝: group={group_id}, user={user_id}")

    @on_group_increase
    async def handle_group_increase(self, event: NoticeEvent):
        """处理入群事件"""
        group_id = event.group_id
        user_id = event.user_id
        join_type = event.sub_type  # approve/invite

        rule = self._get_rule(group_id)
        if rule is None or not rule.enabled:
            return

        # 尝试从缓存获取入群回答
        join_answer = None
        for flag, (g, u, comment) in list(self.pending_requests.items()):
            if g == group_id and u == user_id:
                join_answer = comment
                del self.pending_requests[flag]
                break

        # 记录入群
        self.db.add_join_record(
            user_id=user_id,
            group_id=group_id,
            join_time=event.time,
            join_answer=join_answer,
            join_type=join_type,
        )
        logger.info(f"入群记录: group={group_id}, user={user_id}")

    @on_notice
    async def handle_group_decrease(self, event: NoticeEvent):
        """处理退群事件"""
        if event.notice_type != "group_decrease":
            return

        group_id = event.group_id
        user_id = event.user_id
        leave_type = event.sub_type  # leave/kick/kick_me

        rule = self._get_rule(group_id)
        if rule is None or not rule.enabled:
            return

        self.db.update_leave_record(
            user_id=user_id,
            group_id=group_id,
            leave_time=event.time,
            leave_type=leave_type,
        )
        logger.info(f"退群记录: group={group_id}, user={user_id}")

    # ========== 管理命令 ==========

    @command_registry.command("ga_enable", description="[管理员] 启用本群群管功能")
    async def cmd_enable(self, event: GroupMessageEvent):
        """启用群管功能"""
        group_id = str(event.group_id)
        rule = self._get_or_create_rule(group_id)
        rule.enabled = True
        self._save_config()
        await event.reply("群管功能已启用")

    @command_registry.command("ga_disable", description="[管理员] 禁用本群群管功能")
    async def cmd_disable(self, event: GroupMessageEvent):
        """禁用群管功能"""
        group_id = str(event.group_id)
        rule = self._get_rule(group_id)
        if rule:
            rule.enabled = False
            self._save_config()
        await event.reply("群管功能已禁用")

    @command_registry.command("ga_pattern", description="[管理员] 设置入群验证正则")
    @param(name="pattern", default="", help="正则表达式，留空则清除")
    async def cmd_pattern(self, event: GroupMessageEvent, pattern: str = ""):
        """设置入群验证正则"""
        group_id = str(event.group_id)
        rule = self._get_or_create_rule(group_id)
        rule.pattern = pattern
        self._save_config()
        if pattern:
            await event.reply(f"入群验证正则已设置: {pattern}")
        else:
            await event.reply("入群验证正则已清除")

    @command_registry.command("ga_reject", description="[管理员] 设置自动拒绝")
    @param(name="enabled", default=True, help="是否启用自动拒绝")
    @param(name="reason", default="回答不正确", help="拒绝理由")
    async def cmd_reject(
        self, event: GroupMessageEvent, enabled: bool = True, reason: str = "回答不正确"
    ):
        """设置自动拒绝"""
        group_id = str(event.group_id)
        rule = self._get_or_create_rule(group_id)
        rule.auto_reject = enabled
        rule.reject_reason = reason
        self._save_config()
        status = "启用" if enabled else "禁用"
        await event.reply(f"自动拒绝已{status}，理由: {reason}")

    @command_registry.command("ga_status", description="查看本群群管状态")
    async def cmd_status(self, event: GroupMessageEvent):
        """查看群管状态"""
        group_id = str(event.group_id)
        rule = self._get_rule(group_id)
        if rule is None or not rule.enabled:
            await event.reply("群管功能未启用")
            return
        lines = [
            "群管状态:",
            f"  正则: {rule.pattern or '未设置'}",
            f"  自动拒绝: {'是' if rule.auto_reject else '否'}",
        ]
        await event.reply("\n".join(lines))

    @command_registry.command("ga_query", description="[管理员] 查询成员记录")
    @param(name="user_id", default=None, help="用户QQ号，不填则查询最近记录")
    async def cmd_query(self, event: GroupMessageEvent, user_id: str = None):
        """查询成员记录"""
        from datetime import datetime

        group_id = str(event.group_id)
        records = self.db.get_member_records(group_id, user_id)

        if not records:
            await event.reply("无记录")
            return

        # 只显示最近10条
        records = records[-10:]
        lines = ["成员记录:"]
        for r in records:
            join_time = datetime.fromtimestamp(r.join_time).strftime("%m-%d %H:%M") if r.join_time else "?"
            leave_time = datetime.fromtimestamp(r.leave_time).strftime("%m-%d %H:%M") if r.leave_time else "-"
            answer = (r.join_answer[:20] + "...") if r.join_answer and len(r.join_answer) > 20 else (r.join_answer or "")
            lines.append(f"  {r.user_id}: {join_time}→{leave_time} [{answer}]")

        await event.reply("\n".join(lines))


__all__ = ["GroupAdminPlugin"]
