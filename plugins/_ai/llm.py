"""LLM 客户端 —— OpenAI 兼容 API"""

import httpx
from ncatbot.utils import get_log

logger = get_log("AiMod")


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.1,
        max_tokens: int = 500,
    ) -> str | None:
        """发送聊天请求，返回回复文本。失败返回 None。"""
        if not self.configured:
            logger.error("LLM 未配置")
            return None

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        last_error = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code != 200:
                        last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                        logger.error(f"LLM 请求失败 (attempt {attempt + 1}): {last_error}")
                        continue
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    return content.strip()
            except Exception as e:
                last_error = str(e)
                logger.error(f"LLM 请求异常 (attempt {attempt + 1}): {last_error}")

        logger.error(f"LLM 请求全部重试失败: {last_error}")
        return None

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
            user_prompt += f"\n\n群聊上下文（其他群友的反应）：\n{context}"
        user_prompt += "\n\n请先回答「是」或「否」，然后简要说明理由（不超过30字）。"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        reply = await self.chat(messages, temperature=0.1, max_tokens=100)
        if reply is None:
            return False, "LLM 请求失败"

        is_sensitive = reply.strip().startswith("是")
        reason = reply.strip()
        return is_sensitive, reason

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
