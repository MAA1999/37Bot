"""群聊总结插件"""

import asyncio
import json
from datetime import datetime

from ncatbot.plugin_system import NcatBotPlugin, command_registry, param, on_message
from ncatbot.core.event import GroupMessageEvent
from ncatbot.utils import get_log

from plugins._ai import get_llm, is_llm_configured
from .config import SummaryGroupConfig

logger = get_log("Summary")

SUMMARY_SYSTEM = (
    "你是群聊总结助手。根据提供的群聊记录，总结讨论内容。\n\n"
    "输出格式：\n"
    "📊 群聊总结 ({msg_count} 条消息)\n\n"
    "💬 讨论话题\n"
    "- 话题1：简要描述（参与人）\n"
    "- 话题2：...\n\n"
    "🔑 关键结论\n"
    "- 要点1\n"
    "- 要点2\n\n"
    "要求：简洁，每个话题一句话。无实质讨论则不编造。"
)

ISSUE_TRACK_PROMPT = (
    "\n\n另外，群里有人提到项目使用疑问或遇到了 bug/报错时，请在末尾追加：\n\n"
    "🐛 问题追踪\n\n"
    "每条问题格式：\n"
    "- [时间 HH:MM] 报告者：问题描述 | 证据：有（日志/截图/报错信息简述）或无\n\n"
    "要求：只记录明确的问题报告，闲聊不算。"
)


class GroupSummaryPlugin(NcatBotPlugin):
    name = "GroupSummaryPlugin"
    version = "1.0.0"
    author = "Windsland52"
    dependencies = {}

    async def on_load(self):
        self.config_path = self.workspace / "config.json"
        self.groups: dict[str, SummaryGroupConfig] = self._load_config()
        self._name_cache: dict[str, str] = {}
        asyncio.create_task(self._auto_summary_loop())

    # ====== 配置 ======

    def _load_config(self) -> dict[str, SummaryGroupConfig]:
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text("utf-8"))
                return {
                    gid: SummaryGroupConfig(**{k: v for k, v in g.items() if k in SummaryGroupConfig.__dataclass_fields__})
                    for gid, g in data.items()
                }
            except Exception:
                pass
        return {}

    def _save_config(self):
        self.config_path.write_text(
            json.dumps(
                {gid: {
                    "enabled": g.enabled, "auto": g.auto, "auto_hour": g.auto_hour,
                    "message_count": g.message_count, "track_issues": g.track_issues,
                    "last_summary_date": g.last_summary_date,
                } for gid, g in self.groups.items()},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )

    def _get_cfg(self, group_id: str) -> SummaryGroupConfig:
        if group_id not in self.groups:
            self.groups[group_id] = SummaryGroupConfig()
        return self.groups[group_id]

    async def _is_group_admin(self, group_id: str, user_id: str) -> bool:
        try:
            info = await self.api.get_group_member_info(group_id, user_id)
            return info.role in ("owner", "admin")
        except Exception as e:
            logger.error(f"get_group_member_info error: {e}")
            return False

    async def _resolve_user_name(self, group_id: str, user_id: str) -> str:
        cache_key = f"{group_id}_{user_id}"
        if cache_key in self._name_cache:
            return self._name_cache[cache_key]
        try:
            info = await self.api.get_group_member_info(group_id, user_id)
            name = info.card or info.nickname or user_id
        except Exception:
            name = user_id
        display = f"{name}({user_id})"
        self._name_cache[cache_key] = display
        return display

    # ====== 核心 ======

    async def _do_summary(self, group_id: str, cfg: SummaryGroupConfig) -> str | None:
        try:
            recent = await self.api.get_group_msg_history(group_id, count=cfg.message_count)
        except Exception as e:
            logger.error(f"获取消息历史失败: {e}")
            return None

        if not recent:
            return None

        lines = []
        for m in reversed(recent):
            name = await self._resolve_user_name(group_id, str(m.user_id))
            lines.append(f"[{m.time}] [{name}]: {m.raw_message}")
        chat_text = "\n".join(lines)

        system_prompt = SUMMARY_SYSTEM.format(msg_count=len(recent))
        if cfg.track_issues:
            system_prompt += ISSUE_TRACK_PROMPT

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"请总结以下群聊：\n\n{chat_text}"},
        ]

        reply = await get_llm().chat(messages, temperature=0.3, max_tokens=2000)
        return reply

    # ====== 定时任务 ======

    async def _auto_summary_loop(self):
        await asyncio.sleep(30)
        while True:
            await asyncio.sleep(300)  # 每 5 分钟检查
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            for group_id, cfg in self.groups.items():
                if not cfg.enabled or not cfg.auto:
                    continue
                if cfg.last_summary_date == today:
                    continue
                if now.hour != cfg.auto_hour:
                    continue
                # 只在目标小时的第一个 5 分钟内触发
                if now.minute >= 10:
                    continue
                if not is_llm_configured():
                    continue
                logger.info(f"定时总结: group={group_id}")
                reply = await self._do_summary(group_id, cfg)
                if reply:
                    await self.api.post_group_msg(group_id, text=reply)
                cfg.last_summary_date = today
                self._save_config()

    # ====== 命令 ======

    @command_registry.command("summary", description="总结最近群聊 [消息数]")
    @param(name="count", default="200", help="总结的消息数量")
    async def cmd_summary(self, event: GroupMessageEvent, count: str = "200"):
        group_id = str(event.group_id)
        cfg = self._get_cfg(group_id)
        if not is_llm_configured():
            await event.reply("LLM 尚未配置")
            return
        try:
            n = int(count)
        except ValueError:
            n = 200
        cfg.message_count = max(20, min(n, 2000))
        await event.reply(f"正在总结最近 {cfg.message_count} 条消息...")
        reply = await self._do_summary(group_id, cfg)
        if reply:
            await event.reply(reply)
        else:
            await event.reply("总结失败，请稍后重试")

    @command_registry.command("summary_on", description="[管理员] 开启每日定时总结")
    async def cmd_on(self, event: GroupMessageEvent):
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要群主或管理员权限")
            return
        cfg = self._get_cfg(str(event.group_id))
        cfg.enabled = True
        cfg.auto = True
        self._save_config()
        await event.reply(f"定时总结已开启 (每日 {cfg.auto_hour}:00)")

    @command_registry.command("summary_off", description="[管理员] 关闭每日定时总结")
    async def cmd_off(self, event: GroupMessageEvent):
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要群主或管理员权限")
            return
        cfg = self._get_cfg(str(event.group_id))
        cfg.enabled = False
        cfg.auto = False
        self._save_config()
        await event.reply("定时总结已关闭")

    @command_registry.command("summary_time", description="[管理员] 设定时小时 (0-23)")
    @param(name="hour", default="22", help="整点小时数")
    async def cmd_time(self, event: GroupMessageEvent, hour: str = "22"):
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要群主或管理员权限")
            return
        try:
            h = int(hour)
            if not 0 <= h <= 23:
                raise ValueError
        except ValueError:
            await event.reply("请输入 0-23 的整数")
            return
        cfg = self._get_cfg(str(event.group_id))
        cfg.auto_hour = h
        self._save_config()
        await event.reply(f"定时总结时间已设为每日 {h}:00")

    @command_registry.command("summary_count", description="[管理员] 设定总结消息数 (20-2000)")
    @param(name="count", default="200", help="消息数量")
    async def cmd_count(self, event: GroupMessageEvent, count: str = "200"):
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要群主或管理员权限")
            return
        try:
            n = int(count)
            n = max(20, min(n, 2000))
        except ValueError:
            await event.reply("请输入数字")
            return
        cfg = self._get_cfg(str(event.group_id))
        cfg.message_count = n
        self._save_config()
        await event.reply(f"总结消息数已设为 {n}")

    @command_registry.command("summary_track", description="[管理员] 问题追踪 on/off")
    @param(name="action", default="on", help="on 或 off")
    async def cmd_track(self, event: GroupMessageEvent, action: str = "on"):
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要群主或管理员权限")
            return
        cfg = self._get_cfg(str(event.group_id))
        cfg.track_issues = action.lower() == "on"
        self._save_config()
        await event.reply(f"问题追踪已{'启用' if cfg.track_issues else '禁用'}")

    @command_registry.command("summary_status", description="查看本群总结配置")
    async def cmd_status(self, event: GroupMessageEvent):
        cfg = self.groups.get(str(event.group_id))
        lines = [
            "群聊总结:",
            f"  定时: {'开启 (每日 %d:00)' % cfg.auto_hour if cfg and cfg.auto else '关闭'}",
            f"  消息数: {cfg.message_count if cfg else 200}",
            f"  问题追踪: {'启用' if cfg and cfg.track_issues else '禁用'}",
        ]
        if cfg and cfg.last_summary_date:
            lines.append(f"  上次总结: {cfg.last_summary_date}")
        await event.reply("\n".join(lines))


__all__ = ["GroupSummaryPlugin"]
