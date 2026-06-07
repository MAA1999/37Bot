"""LLM 客户端 —— OpenAI 兼容 API"""

import asyncio
import json
import time
import httpx
from ncatbot.utils import get_log
from .config import normalize_openai_base_url

logger = get_log("AiMod")

MAX_CONCURRENT = 3
_semaphore = asyncio.Semaphore(MAX_CONCURRENT)

# ====== 健康追踪 ======

UNHEALTHY_THRESHOLD = 5      # 连续失败 N 次标记不健康
COOLDOWN_SECONDS = 300        # 不健康模型 5 分钟后重试
PROBE_INTERVAL = 300          # 探测间隔 5 分钟

PROFILE_DEFAULTS = {
    "classify": {"temperature": 0.0, "max_tokens": 200, "stream": False, "timeout": 20},
    "answer": {"temperature": 0.3, "max_tokens": 1000, "stream": False, "timeout": 45},
    "summary": {"temperature": 0.3, "max_tokens": 2000, "stream": False, "timeout": 300},
    "vision": {"temperature": 0.1, "max_tokens": 300, "stream": False, "timeout": 60},
    "health": {"temperature": 0.0, "max_tokens": 5, "stream": False, "timeout": 15},
}

_health: dict[str, dict] = {}  # key="url|model" → {failures, last_fail}
_probe_started = False


def _hkey(base_url: str, model: str) -> str:
    return f"{base_url}|{model}"


def _is_healthy(base_url: str, model: str) -> bool:
    entry = _health.get(_hkey(base_url, model))
    if not entry:
        return True
    if entry["failures"] >= UNHEALTHY_THRESHOLD:
        if time.time() - entry["last_fail"] < COOLDOWN_SECONDS:
            return False
        # cooldown 过了，重新给机会
        _health[_hkey(base_url, model)] = {"failures": 0, "last_fail": 0}
    return True


def _mark_success(base_url: str, model: str):
    _health[_hkey(base_url, model)] = {"failures": 0, "last_fail": 0}


def _mark_failure(base_url: str, model: str):
    key = _hkey(base_url, model)
    entry = _health.get(key, {"failures": 0, "last_fail": 0})
    entry["failures"] += 1
    entry["last_fail"] = time.time()
    _health[key] = entry
    if entry["failures"] == UNHEALTHY_THRESHOLD:
        logger.warning(f"模型标记为不健康 (将在 {COOLDOWN_SECONDS}s 后重试): {model}")


def get_health_status() -> dict:
    """返回所有模型健康状态副本"""
    return {
        k: {"failures": v["failures"], "healthy": v["failures"] < UNHEALTHY_THRESHOLD}
        for k, v in _health.items()
    }


def start_health_probe(get_client_fn):
    """启动周期探测（幂等，仅首次调用生效）。get_client_fn 返回 LLMClient。"""
    global _probe_started
    if _probe_started:
        return
    _probe_started = True
    asyncio.create_task(_health_probe_loop(get_client_fn))


async def _health_probe_loop(get_client_fn):
    await asyncio.sleep(60)
    while True:
        await asyncio.sleep(PROBE_INTERVAL)
        client = get_client_fn()
        for label, base_url, api_key, model in client._model_cfgs(include_unhealthy=True):
            if _is_healthy(base_url, model):
                continue  # 健康的不用探测
            logger.info(f"健康探测: {label} {model}")
            msgs = [{"role": "user", "content": "hi"}]
            try:
                async with _semaphore:
                    result, _ = await client._chat_impl(
                        base_url, api_key, model, msgs, 0, 5, False, 15)
                if result is not None:
                    _mark_success(base_url, model)
                    logger.info(f"健康探测恢复: {label} {model}")
            except Exception:
                pass


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, backups: list[dict] | None = None):
        self.base_url = normalize_openai_base_url(base_url)
        self.api_key = api_key
        self.model = model
        self.backups = backups or []

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def _model_cfgs(self, include_unhealthy: bool = False):
        """生成器：主模型 + 备用模型依次产出 (label, base_url, api_key, model)，跳过不健康的"""
        if self.base_url and self.api_key and self.model and (include_unhealthy or _is_healthy(self.base_url, self.model)):
            yield "primary", self.base_url, self.api_key, self.model
        elif self.base_url and self.model:
            logger.info(f"跳过不健康主模型: {self.model}")
        for i, b in enumerate(self.backups):
            bu = normalize_openai_base_url(str(b.get("base_url", "")))
            bm = str(b.get("model", ""))
            key = str(b.get("api_key", ""))
            if not bu or not key or not bm:
                continue
            if include_unhealthy or _is_healthy(bu, bm):
                yield f"backup-{i+1}", bu, key, bm
            else:
                logger.info(f"跳过不健康备用 #{i+1}: {bm}")
        if not self.backups:
            logger.info("无备用模型配置，仅使用主模型")

    async def chat(
        self,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool | None = None,
        timeout: float | None = None,
        profile: str = "answer",
    ) -> str | None:
        """发送聊天请求，返回回复文本。失败返回 None。"""
        if not self.configured:
            logger.error("LLM 未配置")
            return None

        defaults = PROFILE_DEFAULTS.get(profile, PROFILE_DEFAULTS["answer"])
        temperature = defaults["temperature"] if temperature is None else temperature
        max_tokens = defaults["max_tokens"] if max_tokens is None else max_tokens
        stream = defaults["stream"] if stream is None else stream
        timeout = defaults["timeout"] if timeout is None else timeout

        async with _semaphore:
            cfgs = list(self._model_cfgs())
            logger.info(f"可用模型 ({len(cfgs)}): {[l for l,_,_,_ in cfgs]}")
            for label, base_url, api_key, model in cfgs:
                result, is_transient = await self._chat_impl(
                    base_url, api_key, model, messages, temperature, max_tokens, stream, timeout)
                if result is not None:
                    _mark_success(base_url, model)
                    if label != "primary":
                        logger.info(f"主模型失败，{label} 接管成功: {model}")
                    return result
                if not is_transient:
                    _mark_failure(base_url, model)
                next_label = self._next_label(label)
                if next_label is None:
                    logger.error(f"所有模型均失败 ({label})")
                else:
                    logger.warning(f"{label} 失败 → 尝试 {next_label}")
        return None

    def _next_label(self, current: str) -> str | None:
        cfg_list = list(self._model_cfgs())
        for i, (label, _, _, _) in enumerate(cfg_list):
            if label == current and i + 1 < len(cfg_list):
                return cfg_list[i + 1][0]
        return None

    async def _chat_impl(
        self,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        stream: bool,
        timeout: float,
    ) -> tuple[str | None, bool]:  # (content, is_transient_error)
        url = f"{base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }

        transient_error = False
        for attempt in range(3 if stream else 2):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15)) as client:
                    if stream:
                        content = ""
                        lines_received = 0
                        async with client.stream("POST", url, json=payload, headers=headers) as resp:
                            if resp.status_code != 200:
                                status = resp.status_code
                                transient_error = status in (408, 409, 425, 429) or status >= 500
                                logger.error(f"LLM 请求失败 (attempt {attempt + 1}): HTTP {status}")
                                continue
                            async for line in resp.aiter_lines():
                                if line.startswith("data: "):
                                    lines_received += 1
                                    data_str = line[6:]
                                    if data_str == "[DONE]":
                                        break
                                    try:
                                        chunk = json.loads(data_str)
                                        delta = chunk["choices"][0].get("delta", {}).get("content", "")
                                        content += delta
                                    except Exception:
                                        pass
                        result = content.strip()
                        if result:
                            return (result, False)
                        logger.warning(f"{model} HTTP 200 但无内容 (SSE行数={lines_received})")
                        continue
                    else:
                        resp = await client.post(url, json=payload, headers=headers)
                        if resp.status_code != 200:
                            status = resp.status_code
                            transient_error = status in (408, 409, 425, 429) or status >= 500
                            logger.error(f"LLM 请求失败 (attempt {attempt + 1}): HTTP {status}: {resp.text[:200]}")
                            continue
                        data = resp.json()
                        content = data["choices"][0]["message"]["content"]
                        result = content.strip()
                        if result:
                            return (result, False)
                        continue
            except Exception as e:
                ename = type(e).__name__
                transient_error = True
                logger.error(f"LLM 请求异常 ({model} attempt {attempt + 1}): {ename}: {e}")

        return (None, transient_error)

    async def analyze_image(self, image_url: str, hint: str = "") -> str | None:
        """用 OpenAI 兼容多模态格式分析单张图片。"""
        system_prompt = (
            "你是图片内容分析助手。请客观描述图片中与聊天上下文有关的信息。"
            "如果图片像截图，请优先提取可见文字、错误信息、界面状态和关键对象。"
            "回答控制在 80 字以内，不要编造看不见的内容。"
        )
        text = "请简要分析这张图片。"
        if hint:
            text += f"\n聊天文字提示：{hint[:500]}"
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ]
        return await self.chat(messages, profile="vision")

    @staticmethod
    def _load_json_reply(reply: str) -> dict | None:
        text = reply.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end == -1 or end <= start:
                return None
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None

    async def judge_sensitive(
        self,
        message_text: str,
        context: str = "",
        min_confidence: float = 0.85,
    ) -> tuple[bool, str]:
        """判断消息是否包含政治敏感内容。返回 (is_sensitive, reason)。"""
        system_prompt = (
            "你是一个内容审核分类器，只判断【当前消息】是否包含政治敏感风险。"
            "上下文只能用于消歧，绝不能作为当前消息违规证据。"
            "正常政策讨论、社会现象讨论、提醒别人别说、引用前文做劝阻，均不应判定为敏感。"
            "只有当前消息本身明确包含极端政治攻击、分裂主义宣传、反政府煽动等内容才判定为敏感。"
            "\n\n"
            "必须严格输出 JSON，不要有任何额外文字：\n"
            '{"sensitive": false, "confidence": 0.0, "category": "safe", '
            '"evidence": "", "reason": "简要理由"}\n\n'
            "如果 sensitive=true，evidence 必须逐字来自当前消息，不能来自上下文。"
        )
        user_prompt = f"当前消息：\n{message_text}"
        if context:
            user_prompt += f"\n\n群聊上下文（仅用于消歧，不可作为证据）：\n{context}"
        user_prompt += "\n\n请输出 JSON："

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        reply = await self.chat(messages, profile="classify", max_tokens=220)
        if reply is None:
            logger.warning("LLM 请求失败，本轮敏感监测跳过")
            return False, "LLM 请求失败"

        data = self._load_json_reply(reply)
        if not data:
            logger.warning(f"LLM 敏感判定返回非 JSON，按安全处理: {reply[:100]}")
            return False, "LLM 返回非 JSON"

        is_sensitive = bool(data.get("sensitive", False))
        reason = str(data.get("reason", "")).strip() or "未给出原因"
        evidence = str(data.get("evidence", "")).strip()
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0

        if not is_sensitive:
            return False, reason
        if confidence < min_confidence:
            logger.info(f"敏感判定低置信度，忽略: confidence={confidence}, reason={reason}")
            return False, f"低置信度: {reason}"
        if not evidence:
            logger.info(f"敏感判定缺少 evidence，忽略: reason={reason}")
            return False, f"缺少证据: {reason}"
        if evidence not in message_text:
            logger.info(f"敏感判定 evidence 不在当前消息中，忽略: evidence={evidence[:50]}")
            return False, f"证据来自上下文或被改写: {reason}"
        return True, reason

    async def judge_question(self, project: str, message_text: str, context: str = "") -> bool:
        """判断消息是否在询问项目相关问题。返回 True/False。"""
        system_prompt = (
            "你是一个消息分类助手。判断一条QQ群消息是否在【主动提问/求助】关于特定项目的问题。\n\n"
            "## 判断标准\n\n"
            "回答「是」的条件（必须同时满足）：\n"
            "1. 消息是一个独立发起的、明确的提问或求助（而非回复/接话）\n"
            "2. 问的内容与该项目相关（用法、报错、配置、功能、兼容性、安装、部署、API、插件等）\n"
            "3. 消息发送者确实期望得到帮助或答案\n\n"
            "回答「否」的条件（任一满足即否）：\n"
            "- 纯闲聊、感叹、吐槽（如\"这项目真好用\"、\"牛逼\"、\"666\"）\n"
            "- 分享、晒图、通知、公告、推荐\n"
            "- 虽含问号但实为反问/感叹（如\"这也太强了吧？\"、\"真的假的？\"、\"不是吧？\"）\n"
            "- 问的是与项目完全无关的事（日常聊天、天气、游戏、吃啥等）\n"
            "- 回复/接话别人的内容，而非独立发起提问\n"
            "- 纯表情包、图片、链接分享（无实质提问文字）\n"
            "- 自问自答、自言自语\n"
            "- 已经解决的问题（如\"搞定了\"、\"好了没问题了\"）\n"
            "- 在描述自己做了什么（陈述），而不是寻求帮助\n"
            "- 含疑问词但语义是日常闲聊（如\"吃啥\"、\"干嘛呢\"、\"谁来打游戏\"）\n\n"
            "如果不确定，倾向于回答「否」（宁可漏答，不要误答）。\n\n"
            "## 示例\n\n"
            "消息: \"这个项目怎么安装？\" → 是\n"
            "消息: \"为什么启动报错 ModuleNotFoundError？\" → 是\n"
            "消息: \"支持Python3.12吗\" → 是\n"
            "消息: \"插件怎么加载不出来\" → 是\n"
            "消息: \"有没有docker部署教程\" → 是\n"
            "消息: \"配置文件放哪个目录\" → 是\n"
            "消息: \"这功能不错啊\" → 否\n"
            "消息: \"真的假的？\" → 否\n"
            "消息: \"有没有人一起打游戏\" → 否\n"
            "消息: \"哈哈哈笑死\" → 否\n"
            "消息: \"我推荐大家用这个\" → 否\n"
            "消息: \"帮我点个赞\" → 否\n"
            "消息: \"说啥呢\" → 否\n"
            "消息: \"什么都行随便\" → 否\n"
            "消息: \"已经搞定了\" → 否\n"
            "消息: \"应该是吧\" → 否\n\n"
            "只回答「是」或「否」，不要解释。"
        )
        user_prompt = (
            f"项目名：{project}\n\n"
            f"消息内容：\n{message_text}"
        )
        if context:
            user_prompt += f"\n\n群聊上下文（仅供参考，用于理解消息语境）：\n{context}"
        user_prompt += "\n\n判断："

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        reply = await self.chat(messages, profile="classify", max_tokens=10)
        if reply is None:
            return False

        # 严格匹配：只有回复以「是」开头才算通过
        answer = reply.strip()
        return answer.startswith("是")

    async def answer_question(self, question: str, system_prompt: str, context: str = "") -> str | None:
        """回答项目相关问题。"""
        if not system_prompt:
            system_prompt = "你是一个项目知识库助手。请根据你的知识回答问题。如果不知道，直接说不知道。"

        user_content = question
        if context:
            user_content = f"群聊上下文：\n{context}\n\n问题：{question}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        return await self.chat(messages, profile="answer")
