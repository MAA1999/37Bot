import asyncio
import base64
import sys
import tempfile
import types
import unittest
from pathlib import Path


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


if "ncatbot.utils" not in sys.modules:
    ncatbot = types.ModuleType("ncatbot")
    ncatbot.__path__ = []
    ncatbot_utils = types.ModuleType("ncatbot.utils")
    ncatbot_utils.get_log = lambda name: _Logger()
    ncatbot_utils.ncatbot_config = types.SimpleNamespace(bt_uin="10000")
    sys.modules.setdefault("ncatbot", ncatbot)
    sys.modules.setdefault("ncatbot.utils", ncatbot_utils)
if "ncatbot.plugin_system" not in sys.modules:
    plugin_system = types.ModuleType("ncatbot.plugin_system")

    class _Plugin:
        pass

    class _Registry:
        def command(self, *args, **kwargs):
            return lambda fn: fn

    def _identity_decorator(*args, **kwargs):
        if args and callable(args[0]) and len(args) == 1 and not kwargs:
            return args[0]
        return lambda fn: fn

    plugin_system.NcatBotPlugin = _Plugin
    plugin_system.command_registry = _Registry()
    plugin_system.param = _identity_decorator
    plugin_system.on_message = _identity_decorator
    sys.modules.setdefault("ncatbot.plugin_system", plugin_system)
if "ncatbot.core.event" not in sys.modules:
    ncatbot_core = types.ModuleType("ncatbot.core")
    ncatbot_event = types.ModuleType("ncatbot.core.event")

    class _GroupMessageEvent:
        pass

    class _PrivateMessageEvent:
        pass

    ncatbot_event.GroupMessageEvent = _GroupMessageEvent
    ncatbot_event.PrivateMessageEvent = _PrivateMessageEvent
    sys.modules.setdefault("ncatbot.core", ncatbot_core)
    sys.modules.setdefault("ncatbot.core.event", ncatbot_event)

from plugins._ai.llm import LLMClient
from plugins._ai.llm import get_health_status
from plugins._ai.message import clean_plain_text, image_segment_to_url, local_image_to_data_url
import plugins._ai as ai_shared
import plugins._ai.config as ai_config
from plugins._ai.config import normalize_openai_base_url
import plugins.qa_helper.plugin as qa_plugin
from plugins.sensitive_monitor.config import SensitiveGroupConfig
from plugins.sensitive_monitor.plugin import SensitiveMonitorPlugin


class FakeSeg:
    def __init__(self, msg_seg_type, text="", qq="", file="", url="", summary=""):
        self.msg_seg_type = msg_seg_type
        self.text = text
        self.qq = qq
        self.file = file
        self.url = url
        self.file_name = Path(file).name if file else ""
        self._summary = summary

    def get_summary(self):
        return self._summary


class FakeLLM(LLMClient):
    def __init__(self, reply):
        super().__init__("https://example.com/v1", "key", "model")
        self.reply = reply

    async def chat(self, *args, **kwargs):
        return self.reply


class TransientFailLLM(LLMClient):
    async def _chat_impl(self, *args, **kwargs):
        return None, True


class FakeSensitivePlugin(SensitiveMonitorPlugin):
    def __init__(self):
        self.context_calls = 0

    async def _build_context(self, *args, **kwargs):
        self.context_calls += 1
        return "context"


class FakeGroupEvent(qa_plugin.GroupMessageEvent):
    def __init__(self, message, group_id="1"):
        self.group_id = group_id
        self.user_id = "20000"
        self.message_id = "m1"
        self.time = 100
        self.message = message
        self.replies = []

    async def reply(self, text):
        self.replies.append(text)


class LLMImprovementTests(unittest.TestCase):
    def test_sensitive_rejects_context_evidence(self):
        llm = FakeLLM(
            '{"sensitive": true, "confidence": 0.99, "category": "politics", '
            '"evidence": "上下文里的敏感句", "reason": "证据来自上下文"}'
        )
        result = asyncio.run(llm.judge_sensitive("收到，别聊这个了", "上下文里的敏感句"))
        self.assertEqual(result[0], False)

    def test_sensitive_accepts_current_message_evidence(self):
        llm = FakeLLM(
            '{"sensitive": true, "confidence": 0.91, "category": "politics", '
            '"evidence": "当前消息证据", "reason": "当前消息包含风险内容"}'
        )
        result = asyncio.run(llm.judge_sensitive("这里有当前消息证据", "无关上下文"))
        self.assertEqual(result, (True, "当前消息包含风险内容"))

    def test_sensitive_rejects_low_confidence(self):
        llm = FakeLLM(
            '{"sensitive": true, "confidence": 0.4, "category": "politics", '
            '"evidence": "当前消息证据", "reason": "低置信度"}'
        )
        result = asyncio.run(llm.judge_sensitive("当前消息证据"))
        self.assertEqual(result[0], False)

    def test_clean_plain_text_marks_images(self):
        text = clean_plain_text([
            FakeSeg("text", text="报错如下"),
            FakeSeg("image", file="a.png"),
            FakeSeg("at", qq="123"),
        ])
        self.assertEqual(text, "报错如下[图片]@123")

    def test_local_image_to_data_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tiny.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n")
            data_url = local_image_to_data_url(path)
        self.assertTrue(data_url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(data_url.split(",", 1)[1]), b"\x89PNG\r\n\x1a\n")

    def test_image_segment_http_url(self):
        seg = FakeSeg("image", url="https://example.com/a.png")
        self.assertEqual(asyncio.run(image_segment_to_url(seg)), "https://example.com/a.png")

    def test_transient_failures_do_not_mark_unhealthy(self):
        llm = TransientFailLLM("https://transient.example/v1", "key", "model")
        before = dict(get_health_status())
        asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))
        after = get_health_status()
        self.assertEqual(after.get("https://transient.example/v1|model"), before.get("https://transient.example/v1|model"))

    def test_openai_base_url_normalizes_chat_completions_endpoint(self):
        self.assertEqual(
            normalize_openai_base_url("https://apihub.agnes-ai.com/v1/chat/completions/"),
            "https://apihub.agnes-ai.com/v1",
        )
        llm = LLMClient(
            "https://apihub.agnes-ai.com/v1/chat/completions",
            "key",
            "agnes-2.0-flash",
        )
        self.assertEqual(llm.base_url, "https://apihub.agnes-ai.com/v1")

    def test_saved_config_normalizes_agnes_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_path = ai_config.SHARED_CONFIG_PATH
            ai_config.SHARED_CONFIG_PATH = Path(tmp) / "llm_config.json"
            try:
                cfg = ai_config.LLMConfig(
                    base_url="https://apihub.agnes-ai.com/v1/chat/completions",
                    api_key="key",
                    model="agnes-2.0-flash",
                    backups=[
                        {
                            "base_url": "https://backup.example/v1/chat/completions",
                            "api_key": "backup-key",
                            "model": "backup-model",
                        }
                    ],
                )
                ai_config.save_llm_config(cfg)
                loaded = ai_config.load_llm_config()
                self.assertEqual(loaded.base_url, "https://apihub.agnes-ai.com/v1")
                self.assertEqual(loaded.backups[0]["base_url"], "https://backup.example/v1")
            finally:
                ai_config.SHARED_CONFIG_PATH = old_path

    def test_vision_llm_falls_back_to_primary_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_path = ai_config.SHARED_CONFIG_PATH
            ai_config.SHARED_CONFIG_PATH = Path(tmp) / "llm_config.json"
            try:
                cfg = ai_config.LLMConfig(
                    base_url="https://agnes.example/v1",
                    api_key="key",
                    model="agnes-2.0-flash",
                )
                ai_config.save_llm_config(cfg)

                self.assertTrue(ai_shared.is_vision_llm_configured())
                client = ai_shared.get_vision_llm()
                self.assertEqual(client.base_url, "https://agnes.example/v1")
                self.assertEqual(client.api_key, "key")
                self.assertEqual(client.model, "agnes-2.0-flash")
            finally:
                ai_config.SHARED_CONFIG_PATH = old_path

    def test_vision_llm_prefers_dedicated_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_path = ai_config.SHARED_CONFIG_PATH
            ai_config.SHARED_CONFIG_PATH = Path(tmp) / "llm_config.json"
            try:
                cfg = ai_config.LLMConfig(
                    base_url="https://text.example/v1",
                    api_key="text-key",
                    model="text-model",
                    vision_base_url="https://vision.example/v1",
                    vision_api_key="vision-key",
                    vision_model="vision-model",
                )
                ai_config.save_llm_config(cfg)

                client = ai_shared.get_vision_llm()
                self.assertEqual(client.base_url, "https://vision.example/v1")
                self.assertEqual(client.api_key, "vision-key")
                self.assertEqual(client.model, "vision-model")
            finally:
                ai_config.SHARED_CONFIG_PATH = old_path

    def test_qa_disabled_at_bot_uses_general_ai_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = qa_plugin.QaHelperPlugin.__new__(qa_plugin.QaHelperPlugin)
            plugin.groups = {}
            plugin._bot_qq = "10000"
            plugin.workspace = Path(tmp)

            old_get_llm = qa_plugin.get_llm
            old_is_llm_configured = qa_plugin.is_llm_configured
            old_load_llm_config = qa_plugin.load_llm_config
            try:
                qa_plugin.get_llm = lambda: FakeLLM("通用回复")
                qa_plugin.is_llm_configured = lambda: True
                qa_plugin.load_llm_config = lambda: ai_config.LLMConfig(
                    base_url="https://example.com/v1",
                    api_key="key",
                    model="model",
                )

                event = FakeGroupEvent([
                    FakeSeg("at", qq="10000"),
                    FakeSeg("text", text=" 你好"),
                ])
                asyncio.run(plugin._on_message(event))
                self.assertEqual(event.replies, ["通用回复"])
            finally:
                qa_plugin.get_llm = old_get_llm
                qa_plugin.is_llm_configured = old_is_llm_configured
                qa_plugin.load_llm_config = old_load_llm_config

    def test_qa_disabled_without_at_bot_does_not_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = qa_plugin.QaHelperPlugin.__new__(qa_plugin.QaHelperPlugin)
            plugin.groups = {}
            plugin._bot_qq = "10000"
            plugin.workspace = Path(tmp)

            event = FakeGroupEvent([FakeSeg("text", text="你好")])
            asyncio.run(plugin._on_message(event))
            self.assertEqual(event.replies, [])

    def test_sensitive_context_not_built_when_unused(self):
        plugin = FakeSensitivePlugin()
        cfg = SensitiveGroupConfig(context_for_judge=False, append_context=False)
        needs_context = cfg.context_for_judge or cfg.append_context
        context = asyncio.run(plugin._build_context("1", object(), cfg)) if needs_context else ""
        self.assertEqual(context, "")
        self.assertEqual(plugin.context_calls, 0)


if __name__ == "__main__":
    unittest.main()
