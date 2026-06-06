import asyncio
import json
import time
from pathlib import Path

from ncatbot.core.message import GroupMessage, PrivateMessage
from ncatbot.plugin import CompatibleEnrollment, BasePlugin
from ncatbot.utils import get_log

from .config import load_llm_config, save_llm_config
from . import get_llm, is_llm_configured, start_llm_health_probe
from .utils import looks_like_question, extract_question_text, build_messages

bot = CompatibleEnrollment


# ====== 插件主体 ======


class AiPlugin(BasePlugin):
    name = "AiPlugin"
    version = "0.4.0"
    _cooldowns: dict = {}

    async def on_load(self):
        self.log = get_log("AiMod")
        start_llm_health_probe()
        self.log.info("AI 插件已加载")

    def _qa_enabled_for_group(self, group_id: str) -> bool:
        """Q&A 启用群由 QaHelperPlugin 统一路由，避免双回复。"""
        cfg_path = Path("data/QaHelperPlugin/config.json")
        if not cfg_path.exists():
            return False
        try:
            data = json.loads(cfg_path.read_text("utf-8"))
            group_cfg = data.get(str(group_id), {})
            return bool(group_cfg.get("enabled") and group_cfg.get("projects"))
        except Exception:
            return False

    # ---------- 群聊 ----------

    @bot.group_event()
    async def on_group_message(self, msg: GroupMessage):
        raw = str(msg.raw_message).strip()
        cfg = load_llm_config()

        if not cfg.enabled:
            return
        if not is_llm_configured():
            return
        if self._qa_enabled_for_group(str(msg.group_id)):
            return

        # @机器人 → 直接回复
        if f"[CQ:at,qq={msg.self_id}]" in raw:
            question = extract_question_text(raw)
            if not question:
                return
            await self._answer(msg, question, cfg)
            return

        # 非 @ 消息：自动识别提问
        if not cfg.auto_reply:
            return
        if not looks_like_question(raw):
            return
        question = extract_question_text(raw)
        if not question:
            return

        cd = cfg.cooldown
        if cd > 0:
            now = time.time()
            gid = str(msg.group_id)
            if now - self._cooldowns.get(gid, 0) < cd:
                return
            self._cooldowns[gid] = now

        await self._answer(msg, question, cfg)

    # ---------- 私聊 ----------

    @bot.private_event()
    async def on_private_message(self, msg: PrivateMessage):
        raw = str(msg.raw_message).strip()
        cfg = load_llm_config()

        if not cfg.enabled:
            return
        if not is_llm_configured():
            return

        question = extract_question_text(raw)
        if not question:
            return
        await self._answer(msg, question, cfg)

    # ---------- 公共回答 ----------

    async def _answer(self, msg, question: str, cfg):
        client = get_llm()
        messages = build_messages(cfg.ai_system_prompt, [], question)

        try:
            answer = await asyncio.wait_for(
                client.chat(messages),
                timeout=cfg.timeout,
            )
        except asyncio.TimeoutError:
            answer = "⏳ AI 响应超时，请稍后再试"
        except Exception as e:
            self.log.warning(f"AI 回答失败: {e}")
            answer = f"❌ AI 暂时不可用: {e}"

        await msg.reply(answer)
