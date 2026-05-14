"""项目知识库 Q&A 插件"""

import asyncio
import json
import re

import httpx

from ncatbot.plugin_system import NcatBotPlugin, command_registry, param, on_message
from ncatbot.core.event import GroupMessageEvent, PrivateMessageEvent
from ncatbot.utils import get_log, ncatbot_config

from plugins._ai import get_llm, is_llm_configured, load_llm_config, save_llm_config
from .config import QAGroupConfig

logger = get_log("QA")

DOCS_URLS: dict[str, list[str]] = {
    "m9a": [
        "https://raw.githubusercontent.com/MAA1999/M9A/main/docs/zh_cn/manual/faq.md",
        "https://raw.githubusercontent.com/MAA1999/M9A/main/docs/zh_cn/manual/introduction.md",
        "https://raw.githubusercontent.com/MAA1999/M9A/main/docs/zh_cn/manual/newbie.md",
        "https://raw.githubusercontent.com/MAA1999/M9A/main/docs/zh_cn/manual/connection.md",
        "https://raw.githubusercontent.com/MAA1999/M9A/main/docs/zh_cn/manual/cli.md",
        "https://raw.githubusercontent.com/MAA1999/M9A/main/docs/zh_cn/manual/MirrorChyan.md",
    ],
    "maaend": [
        "https://raw.githubusercontent.com/MaaEnd/MaaEnd/v2/docs/zh_cn/users/troubleshooting.md",
    ],
}


class QaHelperPlugin(NcatBotPlugin):
    name = "QaHelperPlugin"
    version = "1.0.0"
    author = "Windsland52"
    dependencies = {}

    async def on_load(self):
        self.config_path = self.workspace / "config.json"
        self.groups: dict[str, QAGroupConfig] = self._load_config()
        self._bot_qq: str | None = None

    def _load_config(self) -> dict[str, QAGroupConfig]:
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text("utf-8"))
                return {
                    gid: QAGroupConfig(
                        enabled=g.get("enabled", False),
                        project=g.get("project", ""),
                        system_prompt=g.get("system_prompt", ""),
                    )
                    for gid, g in data.items()
                }
            except Exception:
                pass
        return {}

    def _save_config(self):
        self.config_path.write_text(
            json.dumps({
                gid: {"enabled": g.enabled, "project": g.project, "system_prompt": g.system_prompt}
                for gid, g in self.groups.items()
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def _is_group_admin(self, group_id: str, user_id: str) -> bool:
        try:
            info = await self.api.get_group_member_info(group_id, user_id)
            return info.role in ("owner", "admin")
        except Exception as e:
            logger.error(f"get_group_member_info error: {e}")
            return False

    async def _get_bot_qq(self) -> str:
        if self._bot_qq is None:
            self._bot_qq = str(ncatbot_config.bt_uin)
        return self._bot_qq

    # ====== 提示词 ======

    def _get_system_prompt(self, cfg: QAGroupConfig) -> str:
        if cfg.system_prompt:
            return cfg.system_prompt
        if cfg.project:
            cache_path = self.workspace / f"cache_{cfg.project.lower()}.txt"
            if cache_path.exists():
                try:
                    return cache_path.read_text("utf-8").strip()
                except Exception:
                    pass
        return ""

    @staticmethod
    def _strip_frontmatter(text: str) -> str:
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                text = text[end + 3:].lstrip("\n")
        return text

    async def _fetch_docs(self, project: str) -> str | None:
        urls = DOCS_URLS.get(project.lower(), [])
        if not urls:
            return None

        async def fetch_one(url: str) -> str | None:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        return resp.text
                    logger.error(f"抓取失败 {url}: HTTP {resp.status_code}")
            except Exception as e:
                logger.error(f"抓取失败 {url}: {e}")
            return None

        results = await asyncio.gather(*[fetch_one(u) for u in urls])
        contents = [self._strip_frontmatter(r) for r in results if r]
        if not contents:
            return None

        combined = "\n\n---\n\n".join(contents)
        return (
            f"你是 {project} 项目助手。请根据以下文档内容回答用户的问题。\n"
            f"回答应简洁准确。对于不知道的问题，直接说不知道，不要编造。\n\n"
            f"---\n\n"
            f"{combined}"
        )

    # ====== Q&A 处理 ======

    @staticmethod
    def _looks_like_question(text: str) -> bool:
        indicators = [
            "?", "？", "怎么", "如何", "为什么", "能不能", "可以不",
            "有没有", "是什么", "是谁", "在哪", "怎么办", "请问",
            "问一下", "请教", "求助", "帮我看", "帮我",
            "啥", "吗", "呢", "吧",
        ]
        return any(i in text for i in indicators)

    @on_message
    async def _on_message(self, event):
        if not isinstance(event, GroupMessageEvent):
            return

        group_id = str(event.group_id)
        cfg = self.groups.get(group_id)
        if not cfg or not cfg.enabled:
            return

        text = (event.raw_message or "").strip()
        if not text or text.startswith("/"):
            return

        # @bot 检查
        bot_qq = await self._get_bot_qq()
        is_at_bot = False
        for seg in event.message:
            if getattr(seg, "msg_seg_type", None) == "at":
                qq = str(getattr(seg, "qq", "") or getattr(seg, "user_id", ""))
                if qq == bot_qq:
                    is_at_bot = True
                    break

        question = self._clean_question(event)

        if not is_at_bot:
            if not question or not self._looks_like_question(question):
                return
            if not is_llm_configured():
                return
            ctx = ""
            try:
                recent = await self.api.get_group_msg_history(group_id, count=6)
                prev = [
                    m for m in recent
                    if m.time < event.time and m.message_id != event.message_id
                ]
                if prev:
                    ctx = "\n".join(
                        f"[{m.user_id}]: {m.raw_message}" for m in reversed(prev[-3:])
                    )
            except Exception:
                pass
            if not await get_llm().judge_question(cfg.project, question, ctx):
                return
            logger.info(f"QA 触发（LLM判定）: group={group_id}, question={question[:100]}")

        if not question:
            await event.reply("请问具体问题是什么？")
            return

        if not is_llm_configured():
            await event.reply("LLM 尚未配置，请联系管理员。")
            return

        system_prompt = self._get_system_prompt(cfg)
        answer = await get_llm().answer_question(question, system_prompt)
        if answer:
            await event.reply(answer)
        else:
            await event.reply("抱歉，暂时无法回答这个问题。")

    def _clean_question(self, event: GroupMessageEvent) -> str:
        parts = []
        for seg in event.message:
            seg_type = getattr(seg, "msg_seg_type", None)
            if seg_type in ("text", "plain"):
                t = getattr(seg, "text", "") or ""
                if t:
                    parts.append(t)
        text = " ".join(parts).strip()
        text = re.sub(r'^/\S+\s*', '', text)
        return text.strip()

    # ====== 管理命令 ======

    def _get_cfg(self, group_id: str) -> QAGroupConfig:
        if group_id not in self.groups:
            self.groups[group_id] = QAGroupConfig()
        return self.groups[group_id]

    @command_registry.command("qa_llm", description="[root] 配置 LLM API（私聊，全局共享）")
    async def cmd_llm(self, event: PrivateMessageEvent, base_url: str, api_key: str, model: str):
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

    @command_registry.command("qa", description="[管理员] Q&A 问答 on/off [项目名]")
    @param(name="action", default="on", help="on 或 off")
    @param(name="project", default="", help="项目名 M9A 或 MaaEnd")
    async def cmd_enable(self, event: GroupMessageEvent, action: str = "on", project: str = ""):
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要群主或管理员权限")
            return
        group_id = str(event.group_id)
        cfg = self._get_cfg(group_id)
        if action.lower() == "on":
            if not project:
                await event.reply("请指定项目名，如 /qa on M9A")
                return
            cfg.enabled = True
            cfg.project = project
            self._save_config()
            await event.reply(f"Q&A 已启用: {project}，正在抓取文档...")
            prompt = await self._fetch_docs(project)
            if prompt:
                cache_path = self.workspace / f"cache_{project.lower()}.txt"
                cache_path.write_text(prompt, encoding="utf-8")
                size_kb = len(prompt.encode("utf-8")) / 1024
                await event.reply(f"文档抓取完成 ({size_kb:.0f}KB)，Q&A 就绪")
            else:
                await event.reply("文档抓取失败，请稍后 /qa_refresh 重试")
        else:
            cfg.enabled = False
            self._save_config()
            await event.reply("Q&A 已禁用")

    @command_registry.command("qa_refresh", description="[管理员] 重新抓取项目文档")
    async def cmd_refresh(self, event: GroupMessageEvent):
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要群主或管理员权限")
            return
        group_id = str(event.group_id)
        cfg = self.groups.get(group_id)
        if not cfg or not cfg.project:
            await event.reply("本群未启用 Q&A")
            return
        await event.reply(f"正在重新抓取 {cfg.project} 文档...")
        prompt = await self._fetch_docs(cfg.project)
        if prompt:
            cache_path = self.workspace / f"cache_{cfg.project.lower()}.txt"
            cache_path.write_text(prompt, encoding="utf-8")
            size_kb = len(prompt.encode("utf-8")) / 1024
            await event.reply(f"文档刷新完成 ({size_kb:.0f}KB)")
        else:
            await event.reply("文档抓取失败，缓存未更新")

    @command_registry.command("qa_prompt", description="[管理员] 设置/清除自定义提示词")
    @param(name="prompt", default="", help="系统提示词，留空清除（回退到文档缓存）")
    async def cmd_prompt(self, event: GroupMessageEvent, prompt: str = ""):
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要群主或管理员权限")
            return
        group_id = str(event.group_id)
        cfg = self._get_cfg(group_id)
        if not prompt:
            cfg.system_prompt = ""
            self._save_config()
            cache_path = self.workspace / f"cache_{cfg.project.lower()}.txt" if cfg.project else None
            if cache_path and cache_path.exists():
                await event.reply("提示词已清除，将使用文档缓存")
            else:
                await event.reply("提示词已清除（无回退源，请 /qa_refresh）")
        else:
            cfg.system_prompt = prompt
            self._save_config()
            await event.reply(f"自定义提示词已设置 ({len(prompt)} 字符)")

    @command_registry.command("qa_status", description="查看本群 Q&A 配置")
    async def cmd_status(self, event: GroupMessageEvent):
        group_id = str(event.group_id)
        cfg = self.groups.get(group_id)
        lines = [
            "Q&A 问答:",
            f"  状态: {'启用' if cfg and cfg.enabled else '禁用'}",
        ]
        if cfg and cfg.enabled:
            lines.append(f"  项目: {cfg.project}")
            if cfg.system_prompt:
                lines.append(f"  提示词: 自定义 ({len(cfg.system_prompt)} 字符)")
            else:
                cache_path = self.workspace / f"cache_{cfg.project.lower()}.txt" if cfg.project else None
                if cache_path and cache_path.exists():
                    size = len(cache_path.read_text("utf-8").encode("utf-8")) / 1024
                    lines.append(f"  提示词: 文档缓存 ({size:.0f}KB)")
                else:
                    lines.append(f"  提示词: 未抓取")
        llm_cfg = load_llm_config()
        lines.append(f"LLM: {'已配置' if llm_cfg.base_url else '未配置'}")
        await event.reply("\n".join(lines))


__all__ = ["QaHelperPlugin"]
