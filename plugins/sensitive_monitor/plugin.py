"""敏感消息监听插件"""

import hashlib
import json
import time

from ncatbot.plugin_system import NcatBotPlugin, command_registry, param, on_message
from ncatbot.core.event import GroupMessageEvent, PrivateMessageEvent
from ncatbot.utils import get_log

from plugins._ai import get_llm, is_llm_configured, load_llm_config, save_llm_config
from plugins._ai.message import clean_plain_text
from .config import SensitiveGroupConfig

logger = get_log("Sensitive")

RECENT_PROCESSED: set[str] = set()
RECENT_SENSITIVE: set[str] = set()
MAX_RECENT = 500
MIN_TEXT_LENGTH = 4
LOW_SIGNAL_PATTERN = (
    "收到", "了解", "好的", "ok", "OK", "嗯", "啊", "对", "是的", "确实",
    "别说了", "别聊了", "刚才那句", "上面那句", "撤回吧",
)


def _message_key(group_id: str, message_id) -> str:
    return f"{group_id}:{message_id}"


def _fingerprint(group_id: str, text: str) -> str:
    digest = hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:16]
    return f"{group_id}:fp:{digest}"


def _is_low_signal(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if len(stripped) <= 3:
        return True
    return stripped in LOW_SIGNAL_PATTERN


class SensitiveMonitorPlugin(NcatBotPlugin):
    name = "SensitiveMonitorPlugin"
    version = "1.0.0"
    author = "Windsland52"
    dependencies = {}

    async def on_load(self):
        self.config_path = self.workspace / "config.json"
        self.recent_sensitive_path = self.workspace / "recent_sensitive.json"
        self._notify_cooldowns: dict[str, float] = {}
        self.groups: dict[str, SensitiveGroupConfig] = self._load_config()
        self._load_recent_sensitive()

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
                        review_mode=g.get("review_mode", "balanced"),
                        min_confidence=float(g.get("min_confidence", 0.85)),
                        notify_cooldown=int(g.get("notify_cooldown", 300)),
                        context_for_judge=g.get("context_for_judge", False),
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
                        "review_mode": g.review_mode,
                        "min_confidence": g.min_confidence,
                        "notify_cooldown": g.notify_cooldown,
                        "context_for_judge": g.context_for_judge,
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

    def _load_recent_sensitive(self):
        if not self.recent_sensitive_path.exists():
            return
        try:
            data = json.loads(self.recent_sensitive_path.read_text("utf-8"))
            for item in data.get("items", []):
                if isinstance(item, str):
                    RECENT_SENSITIVE.add(item)
        except Exception as e:
            logger.warning(f"读取敏感消息缓存失败: {e}")

    def _save_recent_sensitive(self):
        try:
            items = sorted(RECENT_SENSITIVE)[-MAX_RECENT:]
            self.recent_sensitive_path.write_text(
                json.dumps({"items": items}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"保存敏感消息缓存失败: {e}")

    def _mark_sensitive(self, group_id: str, message_id, text: str):
        RECENT_SENSITIVE.add(_message_key(group_id, message_id))
        RECENT_SENSITIVE.add(_fingerprint(group_id, text))
        if len(RECENT_SENSITIVE) > MAX_RECENT * 2:
            kept = sorted(RECENT_SENSITIVE)[-MAX_RECENT:]
            RECENT_SENSITIVE.clear()
            RECENT_SENSITIVE.update(kept)
        self._save_recent_sensitive()

    def _was_sensitive(self, group_id: str, message_id, text: str = "") -> bool:
        if _message_key(group_id, message_id) in RECENT_SENSITIVE:
            return True
        return bool(text and _fingerprint(group_id, text) in RECENT_SENSITIVE)

    @staticmethod
    def _needs_llm_review(text: str, cfg: SensitiveGroupConfig) -> bool:
        if cfg.review_mode == "strict":
            return True
        if _is_low_signal(text):
            return False
        # Balanced mode still reviews most substantive messages; it only skips
        # short acknowledgements that commonly inherit risk from prior context.
        return len(text) >= MIN_TEXT_LENGTH

    async def _build_context(self, group_id: str, event, cfg: SensitiveGroupConfig) -> str:
        if cfg.max_context_messages <= 0:
            return ""
        try:
            fetch_count = max(cfg.max_context_messages, 10) + 10
            recent = await self.api.get_group_msg_history(group_id, count=fetch_count)
        except Exception as e:
            logger.error(f"获取消息上下文失败: {e}")
            return ""

        lines = []
        prev = [
            m
            for m in recent
            if m.time < event.time and m.message_id != event.message_id
        ]
        for m in reversed(prev[-cfg.max_context_messages - 5 :]):
            msg_text = clean_plain_text(m.message)
            if not msg_text:
                continue
            if self._was_sensitive(group_id, m.message_id, msg_text):
                continue
            lines.append(f"[{m.user_id}]: {msg_text}")
            if len(lines) >= cfg.max_context_messages:
                break
        return "\n".join(lines)

    # ====== 消息监听 ======

    @on_message
    async def _on_message(self, event):
        if not isinstance(event, GroupMessageEvent):
            return

        group_id = str(event.group_id)
        cfg = self.groups.get(group_id)
        if not cfg or not cfg.enabled:
            return

        text = clean_plain_text(event.message)
        if not text or text.startswith("/"):
            return
        if len(text) < MIN_TEXT_LENGTH:
            return

        if _message_key(group_id, event.message_id) in RECENT_PROCESSED:
            return
        RECENT_PROCESSED.add(_message_key(group_id, event.message_id))
        if len(RECENT_PROCESSED) > MAX_RECENT:
            RECENT_PROCESSED.clear()

        if not is_llm_configured():
            return
        if not self._needs_llm_review(text, cfg):
            return

        needs_context = cfg.context_for_judge or cfg.append_context
        context = await self._build_context(group_id, event, cfg) if needs_context else ""
        judge_context = context if cfg.context_for_judge else ""

        is_sensitive, reason = await get_llm().judge_sensitive(
            text,
            judge_context,
            min_confidence=cfg.min_confidence,
        )
        if is_sensitive:
            self._mark_sensitive(group_id, event.message_id, text)
            logger.info(
                f"敏感消息: group={group_id}, user={event.user_id}, reason={reason}"
            )
            await self._notify(cfg, group_id, str(event.user_id), text, reason, context)

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
        cd_key = f"{group_id}:{user_id}:{text[:80]}"
        now = time.time()
        if cfg.notify_cooldown > 0 and now - self._notify_cooldowns.get(cd_key, 0) < cfg.notify_cooldown:
            logger.info(f"敏感通知冷却中，跳过重复通知: group={group_id}, user={user_id}")
            return
        self._notify_cooldowns[cd_key] = now
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

    @command_registry.command("sensitive_mode", description="[管理员] 敏感监测模式 balanced/strict")
    @param(name="mode", default="balanced", help="balanced 或 strict")
    async def cmd_mode(self, event: GroupMessageEvent, mode: str = "balanced"):
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要群主或管理员权限")
            return
        mode = mode.lower()
        if mode not in ("balanced", "strict"):
            await event.reply("支持: balanced / strict")
            return
        cfg = self._get_cfg(str(event.group_id))
        cfg.review_mode = mode
        self._save_config()
        await event.reply(f"敏感监测模式已设为 {mode}")

    @command_registry.command("sensitive_threshold", description="[管理员] 设置敏感判定阈值 0.50-0.95")
    @param(name="value", default="0.85", help="置信度阈值")
    async def cmd_threshold(self, event: GroupMessageEvent, value: str = "0.85"):
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要群主或管理员权限")
            return
        try:
            threshold = float(value)
            if not 0.50 <= threshold <= 0.95:
                raise ValueError
        except ValueError:
            await event.reply("请输入 0.50-0.95 之间的数字")
            return
        cfg = self._get_cfg(str(event.group_id))
        cfg.min_confidence = threshold
        self._save_config()
        await event.reply(f"敏感判定阈值已设为 {threshold:.2f}")

    @command_registry.command("sensitive_context", description="[管理员] LLM 判定上下文 on/off")
    @param(name="action", default="off", help="on 或 off")
    async def cmd_context(self, event: GroupMessageEvent, action: str = "off"):
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要群主或管理员权限")
            return
        cfg = self._get_cfg(str(event.group_id))
        cfg.context_for_judge = action.lower() == "on"
        self._save_config()
        await event.reply(f"LLM 判定上下文已{'启用' if cfg.context_for_judge else '禁用'}")

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
            lines.append(f"  模式: {cfg.review_mode}")
            lines.append(f"  判定阈值: {cfg.min_confidence:.2f}")
            lines.append(f"  判定上下文: {'是' if cfg.context_for_judge else '否'}")
        llm_cfg = load_llm_config()
        lines.append(f"LLM: {'已配置' if llm_cfg.base_url else '未配置'}")
        await event.reply("\n".join(lines))


__all__ = ["SensitiveMonitorPlugin"]
