"""工具函数"""

import re


# ========== 关键词初筛 ==========

# 强信号关键词：几乎肯定是提问/求助意图
STRONG_KEYWORDS = [
    # 疑问词
    "怎么", "如何", "为什么", "为啥", "咋",
    "能不能", "可不可以", "可以不", "行不行",
    "有没有", "有什么", "有哪些",
    "是什么", "是啥", "啥意思", "什么意思",
    "是谁", "谁知道", "谁能",
    "在哪", "哪里", "哪儿", "哪个",
    "怎么办", "咋办", "咋整", "咋弄",
    "该怎么", "该如何",
    "多少", "几个", "几点", "几天",
    # 求助词
    "请问", "问一下", "请教", "求助", "求问",
    "帮我", "帮忙", "告诉我", "说说",
    # 问题描述
    "报错", "错误", "出错", "异常", "bug",
    "用不了", "打不开", "装不上", "连不上", "起不来",
    "启动不了", "运行不了", "安装不了",
    # 寻求建议
    "推荐", "建议", "哪个好",
    "区别", "对比", "差别", "不同",
    # 英文
    "how", "why", "what", "where", "when", "who",
    "can i", "can you", "could you", "does ", "do you",
    "is there", "are there",
]

# 弱信号：需要配合额外条件才能判定
WEAK_SUFFIX_PARTICLES = ["吗", "呢", "么", "嘛"]  # 仅句尾才算
WEAK_PROBLEM_KEYWORDS = [
    "不行", "不了", "不能", "不会",
    "失败", "方法", "步骤", "教程",
]


def looks_like_question(text: str) -> bool:
    """
    快速判断文本是否 *可能* 是一个提问。
    用于第一层过滤，目标是高召回（宁可多放一些给 LLM 二次判断，也不要漏掉真正的提问）。
    同时通过约束弱信号来避免最明显的误触发。
    """
    text_lower = text.lower().strip()

    # === 排除规则（明显非提问）===
    # 纯 CQ 码（表情/图片/语音）
    if re.fullmatch(r"(\[CQ:[^\]]+\]\s*)+", text_lower):
        return False

    # === 强关键词：命中即通过 ===
    for kw in STRONG_KEYWORDS:
        if kw in text_lower:
            return True

    # === 问号结尾 ===
    if text_lower.endswith("?") or text_lower.endswith("？"):
        return True

    # === 弱信号：语气词仅在句尾才视为疑问 ===
    # 去掉尾部空白和标点后检查
    stripped = re.sub(r"[\s。！!~～…]+$", "", text)
    for particle in WEAK_SUFFIX_PARTICLES:
        if stripped.endswith(particle):
            return True

    # === 弱信号：问题描述词 + 短消息约束 ===
    # 短消息（≤60字符）中出现这些词更可能是求助
    if len(text_lower) <= 60:
        for kw in WEAK_PROBLEM_KEYWORDS:
            if kw in text_lower:
                return True

    return False


def extract_question_text(raw_message: str) -> str:
    """从 raw_message 中提取纯文本（去除 CQ 码等）"""
    # 去掉所有 CQ 码
    text = re.sub(r"\[CQ:[^\]]+\]", "", raw_message)
    return text.strip()
