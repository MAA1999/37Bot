"""LLM 客户端 —— OpenAI 兼容 API"""

import asyncio
import json
import re
import time
import httpx
from ncatbot.utils import get_log

logger = get_log("AiMod")

MAX_CONCURRENT = 3
_semaphore = asyncio.Semaphore(MAX_CONCURRENT)

# ====== 健康追踪 ======

UNHEALTHY_THRESHOLD = 5      # 连续失败 N 次标记不健康
COOLDOWN_SECONDS = 300        # 不健康模型 5 分钟后重试
PROBE_INTERVAL = 300          # 探测间隔 5 分钟

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


async def start_health_probe(get_client_fn):
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
        for label, base_url, api_key, model in client._model_cfgs():
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
    def __init__(self, base_url: str, api_key: str, model: str, backups: list[dict] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.backups = backups or []

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def _model_cfgs(self):
        """生成器：主模型 + 备用模型依次产出 (label, base_url, api_key, model)，跳过不健康的"""
        if _is_healthy(self.base_url, self.model):
            yield "primary", self.base_url, self.api_key, self.model
        else:
            logger.info(f"跳过不健康主模型: {self.model}")
        for i, b in enumerate(self.backups):
            bu = b["base_url"].rstrip("/")
            bm = b["model"]
            if _is_healthy(bu, bm):
                yield f"backup-{i+1}", bu, b["api_key"], bm
            else:
                logger.info(f"跳过不健康备用 #{i+1}: {bm}")
        if not self.backups:
            logger.info("无备用模型配置，仅使用主模型")

    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.1,
        max_tokens: int = 500,
        stream: bool = True,
        timeout: float = 30,
    ) -> str | None:
        """发送聊天请求，返回回复文本。失败返回 None。"""
        if not self.configured:
            logger.error("LLM 未配置")
            return None

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

        last_error = None
        for attempt in range(3 if stream else 2):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15)) as client:
                    if stream:
                        content = ""
                        lines_received = 0
                        async with client.stream("POST", url, json=payload, headers=headers) as resp:
                            if resp.status_code != 200:
                                status = resp.status_code
                                last_error = f"HTTP {status}"
                                logger.error(f"LLM 请求失败 (attempt {attempt + 1}): {last_error}")
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
                        last_error = "EmptyResponse"
                        continue
                    else:
                        resp = await client.post(url, json=payload, headers=headers)
                        if resp.status_code != 200:
                            status = resp.status_code
                            last_error = f"HTTP {status}: {resp.text[:200]}"
                            logger.error(f"LLM 请求失败 (attempt {attempt + 1}): {last_error}")
                            continue
                        data = resp.json()
                        content = data["choices"][0]["message"]["content"]
                        result = content.strip()
                        if result:
                            return (result, False)
                        last_error = "EmptyResponse"
                        continue
            except Exception as e:
                ename = type(e).__name__
                last_error = f"{ename}: {e}"
                logger.error(f"LLM 请求异常 ({model} attempt {attempt + 1}): {last_error}")

        return (None, False)

    async def judge_sensitive(self, message_text: str, context: str = "") -> tuple[bool, str]:
        """判断消息是否包含政治敏感内容。返回 (is_sensitive, reason)。"""
        system_prompt = (
            "你是一个内容审核助手，负责判断 QQ 群消息是否包含政治敏感内容。"
            "注意区分正常政治讨论和恶意敏感言论。"
            "正常讨论（如聊政策影响、社会现象）不应判定为敏感。"
            "只有明显包含极端敏感词、反政府言论、分裂主义宣传等内容才判定为敏感。"
        )
        user_prompt = f"请判断以下 QQ 群消息是否包含政治敏感内容：\n\n消息内容：\n{message_text}"
        if context:
            user_prompt += f"\n\n群聊上下文（此消息之前的对话历史）：\n{context}"
        user_prompt += "\n\n请先回答「是」或「否」，然后简要说明理由（不超过30字）。"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        reply = await self.chat(messages, temperature=0.1, max_tokens=100)
        if reply is None:
            return False, "LLM 请求失败"

        cleaned = reply.strip()
        # Match the first 「是」/「否」 or bare 是/否 at the start
        m = re.search(r'[「（(]?\s*([是否])\s*[」）)]?', cleaned)
        if m:
            is_sensitive = m.group(1) == "是"
        else:
            # Fallback: check first non-whitespace character
            first_char = cleaned.lstrip()[:1] if cleaned else ""
            is_sensitive = first_char == "是"
        return is_sensitive, cleaned[:200]

    async def judge_question(self, project: str, message_text: str, context: str = "") -> bool:
        """判断消息是否在询问项目相关问题。返回 True/False。"""
        system_prompt = (
            "你是一个消息分类助手。你的任务是判断一条 QQ 群消息是否在询问关于特定项目的问题。"
            "只有明确在询问、求助、咨询项目相关问题时才回答「是」。"
            "纯粹的闲聊、感叹、分享、晒图，即使提到项目名，也不算提问。"
        )
        user_prompt = (
            f"项目名：{project}\n\n"
            f"消息内容：\n{message_text}"
        )
        if context:
            user_prompt += f"\n\n群聊上下文：\n{context}"
        user_prompt += "\n\n这条消息是否在询问关于上述项目的问题？只回答「是」或「否」。"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        reply = await self.chat(messages, temperature=0, max_tokens=10)
        if reply is None:
            return False

        return reply.strip().startswith("是")

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

        return await self.chat(messages, temperature=0.3, max_tokens=1000)
