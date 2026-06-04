"""敏感消息监听插件"""

import json
import time

from ncatbot.plugin_system import NcatBotPlugin, command_registry, param, on_message
from ncatbot.core.event import GroupMessageEvent, PrivateMessageEvent
from ncatbot.utils import get_log

from plugins._ai import get_llm, is_llm_configured, load_llm_config, save_llm_config
from .config import SensitiveGroupConfig

logger = get_log("Sensitive")

RECENT_PROCESSED: set[str] = set()
RECENT_SENSITIVE: set[str] = set()
MAX_RECENT = 500
MIN_TEXT_LENGTH = 4
# Per-group cooldown: tracks the last notification time for each group
LAST_NOTIFY_TIME: dict[str, float] = {}
# Prune LAST_NOTIFY_TIME entries older than this (seconds)
NOTIFY_COOLDOWN_PRUNE_AGE = 3600  # 1 hour


def _build_clean_message(message) -> str:
    """Convert a MessageArray to clean text, replacing non-text segments with summaries."""
    parts = []
    for seg in message:
        if seg.msg_seg_type == "text":
            parts.append(seg.text)
        elif seg.msg_seg_type == "at":
            parts.append(f"@{seg.qq}" if seg.qq != "all" else "@全体成员")
        elif seg.msg_seg_type == "reply":
            continue
        else:
            summary = seg.get_summary()
            if summary and summary != "该消息不支持预览":
                parts.append(summary)
    return "".join(parts)


class SensitiveMonitorPlugin(NcatBotPlugin):
    name = "SensitiveMonitorPlugin"
    version = "1.0.0"
    author = "Windsland52"
    dependencies = {}

    async def on_load(self):
        self.config_path = self.workspace / "config.json"
        self.groups: dict[str, SensitiveGroupConfig] = self._load_config()

    def _load_config(self) -> dict[str, SensitiveGroupConfig]:
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text("utf-8"))
                return {
                    gid: SensitiveGroupConfig(
                        enabled=g.get("enabled", False),
                        notify_users=g.get("notify_users", []),
                        warn_in_group=g.get("warn_in_group", False),
                        append_context=g.get("append_context", False),
                        max_context_messages=g.get("max_context_messages", 5),
                        max_context_chars=g.get("max_context_chars", 800),
                        notify_cooldown_seconds=g.get("notify_cooldown_seconds", 120),
                    )
                    for gid, g in data.items()
                }
            except Exception:
                pass
        return {}

    def _save_config(self):
        self.config_path.write_text(
            json.dumps(
                {
                    gid: {
                        "enabled": g.enabled,
                        "notify_users": g.notify_users,
                        "warn_in_group": g.warn_in_group,
                        "append_context": g.append_context,
                        "max_context_messages": g.max_context_messages,
                        "max_context_chars": g.max_context_chars,
                        "notify_cooldown_seconds": g.notify_cooldown_seconds,
                    }
                    for gid, g in self.groups.items()
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    async def _is_group_admin(self, group_id: str, user_id: str) -> bool:
        try:
            info = await self.api.get_group_member_info(group_id, user_id)
            return info.role in ("owner", "admin")
        except Exception as e:
            logger.error(f"get_group_member_info error: {e}")
            return False

    # ====== 消息监听 ======

    @on_message
    async def _on_message(self, event):
        if not isinstance(event, GroupMessageEvent):
            return

        group_id = str(event.group_id)
        cfg = self.groups.get(group_id)
        if not cfg or not cfg.enabled:
            return

        text = event.message.concatenate_text().strip()
        if not text or text.startswith("/"):
            return
        if len(text) < MIN_TEXT_LENGTH:
            return

        if str(event.message_id) in RECENT_PROCESSED:
            return
        RECENT_PROCESSED.add(str(event.message_id))
        if len(RECENT_PROCESSED) > MAX_RECENT:
            RECENT_PROCESSED.clear()
            RECENT_SENSITIVE.clear()
            # Prune stale notification cooldown entries
            now = time.time()
            stale_keys = [
                gid
                for gid, last in LAST_NOTIFY_TIME.items()
                if now - last > NOTIFY_COOLDOWN_PRUNE_AGE
            ]
            for gid in stale_keys:
                del LAST_NOTIFY_TIME[gid]

        if not is_llm_configured():
            return

        context = ""
        try:
            # Fetch more than needed to account for filtered-out messages
            fetch_count = max(cfg.max_context_messages, 10) + 10
            recent = await self.api.get_group_msg_history(group_id, count=fetch_count)
            prev = [
                m
                for m in recent
                if m.time < event.time
                and m.message_id != event.message_id
                and str(m.message_id) not in RECENT_SENSITIVE
            ]
            if prev:
                lines = []
                for m in reversed(prev[-cfg.max_context_messages :]):
                    msg_text = _build_clean_message(m.message)
                    if msg_text:
                        lines.append(f"[{m.user_id}]: {msg_text}")
                context = "\n".join(lines)
        except Exception as e:
            logger.error(f"获取消息上下文失败: {e}")

        is_sensitive, reason = await get_llm().judge_sensitive(text, context)
        if is_sensitive:
            RECENT_SENSITIVE.add(str(event.message_id))
            logger.info(
                f"敏感消息: group={group_id}, user={event.user_id}, reason={reason}"
            )
            # Cooldown check: suppress notification if we recently sent one in this group
            now = time.time()
            last = LAST_NOTIFY_TIME.get(group_id, 0)
            if now - last < cfg.notify_cooldown_seconds:
                logger.info(
                    f"群 {group_id} 处于通知冷却中 (剩余 {int(cfg.notify_cooldown_seconds - (now - last))}s)，跳过通知"
                )
            else:
                await self._notify(
                    cfg, group_id, str(event.user_id), text, reason, context
                )
                LAST_NOTIFY_TIME[group_id] = time.time()

    async def _notify(
        self,
        cfg: SensitiveGroupConfig,
        group_id: str,
        user_id: str,
        text: str,
        reason: str,
        context: str,
    ):
        msg = (
            f"敏感消息提醒\n"
            f"群: {group_id}\n"
            f"发送者: {user_id}\n"
            f"内容: {text}\n"
            f"原因: {reason}"
        )
        if cfg.append_context and context:
            truncated = context
            if len(context) > cfg.max_context_chars:
                truncated = context[:cfg.max_context_chars].rstrip() + "..."
            msg += f"\n对话背景:\n{truncated}"
        for uid in cfg.notify_users:
            try:
                await self.api.post_private_msg(uid, text=msg)
            except Exception as e:
                logger.error(f"私聊通知 {uid} 失败: {e}")
        if cfg.warn_in_group:
            try:
                await self.api.post_group_msg(
                    group_id, text="请注意发言内容，避免发送敏感信息。"
                )
            except Exception as e:
                logger.error(f"群内警告失败: {e}")

    # ====== 管理命令 ======

    def _get_cfg(self, group_id: str) -> SensitiveGroupConfig:
        if group_id not in self.groups:
            self.groups[group_id] = SensitiveGroupConfig()
        return self.groups[group_id]

    @command_registry.command(
        "sensitive_llm", description="[root] 配置 LLM API（私聊，全局共享）"
    )
    async def cmd_llm(
        self, event: PrivateMessageEvent, base_url: str, api_key: str, model: str
    ):
        if event.message_type != "private":
            await event.reply("请私聊使用此命令")
            return
        if not self.rbac_manager.user_has_role(str(event.user_id), "root"):
            await event.reply("需要 root 权限")
            return
        cfg = load_llm_config()
        cfg.base_url = base_url.rstrip("/")
        cfg.api_key = api_key
        cfg.model = model
        save_llm_config(cfg)
        await event.reply(f"LLM 配置已更新: {model} @ {base_url}")

    @command_registry.command("sensitive", description="[管理员] 敏感消息监听 on/off")
    @param(name="action", default="on", help="on 或 off")
    async def cmd_enable(self, event: GroupMessageEvent, action: str = "on"):
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要群主或管理员权限")
            return
        group_id = str(event.group_id)
        cfg = self._get_cfg(group_id)
        cfg.enabled = action.lower() == "on"
        self._save_config()
        await event.reply(f"敏感消息监听已{'启用' if cfg.enabled else '禁用'}")

    @command_registry.command(
        "sensitive_notify", description="[管理员] 通知目标 切换添加/移除"
    )
    @param(name="qq", default="", help="接收通知的 QQ 号")
    async def cmd_notify(self, event: GroupMessageEvent, qq: str = ""):
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要群主或管理员权限")
            return
        if not qq:
            await event.reply("请指定 QQ 号")
            return
        group_id = str(event.group_id)
        cfg = self._get_cfg(group_id)
        if qq in cfg.notify_users:
            cfg.notify_users.remove(qq)
            self._save_config()
            await event.reply(f"已从通知列表移除: {qq}")
        else:
            cfg.notify_users.append(qq)
            self._save_config()
            await event.reply(f"已添加到通知列表: {qq}")

    @command_registry.command("sensitive_warn", description="[管理员] 群内警告 on/off")
    @param(name="action", default="off", help="on 或 off")
    async def cmd_warn(self, event: GroupMessageEvent, action: str = "off"):
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要群主或管理员权限")
            return
        group_id = str(event.group_id)
        cfg = self._get_cfg(group_id)
        cfg.warn_in_group = action.lower() == "on"
        self._save_config()
        await event.reply(f"群内警告已{'启用' if cfg.warn_in_group else '禁用'}")

    @command_registry.command("sensitive_status", description="查看本群敏感词监听配置")
    async def cmd_status(self, event: GroupMessageEvent):
        group_id = str(event.group_id)
        cfg = self.groups.get(group_id)
        lines = [
            "敏感消息监听:",
            f"  状态: {'启用' if cfg and cfg.enabled else '禁用'}",
        ]
        if cfg and cfg.enabled:
            lines.append(
                f"  通知对象: {', '.join(cfg.notify_users) if cfg.notify_users else '无'}"
            )
            lines.append(f"  群内警告: {'是' if cfg.warn_in_group else '否'}")
            lines.append(f"  附加对话背景: {'是' if cfg.append_context else '否'}")
            lines.append(f"  对话消息数上限: {cfg.max_context_messages}")
            lines.append(f"  对话字符上限: {cfg.max_context_chars}")
            lines.append(f"  通知冷却: {cfg.notify_cooldown_seconds}秒")
        llm_cfg = load_llm_config()
        lines.append(f"LLM: {'已配置' if llm_cfg.base_url else '未配置'}")
        await event.reply("\n".join(lines))

    @command_registry.command(
        "sensitive_cooldown", description="[管理员] 设置通知冷却秒数"
    )
    @param(name="seconds", default="120", help="冷却秒数，设为0禁用冷却")
    async def cmd_cooldown(self, event: GroupMessageEvent, seconds: str = "120"):
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要群主或管理员权限")
            return
        try:
            val = int(seconds)
            if val < 0:
                await event.reply("冷却秒数不能为负数")
                return
        except ValueError:
            await event.reply("请输入有效的数字")
            return
        group_id = str(event.group_id)
        cfg = self._get_cfg(group_id)
        cfg.notify_cooldown_seconds = val
        self._save_config()
        if val == 0:
            await event.reply("通知冷却已禁用")
        else:
            await event.reply(f"通知冷却已设置为 {val} 秒")


__all__ = ["SensitiveMonitorPlugin"]
