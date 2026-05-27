"""AI 插件通用工具函数"""

import re



# ====== 消息构建 ======


def format_message(role: str, content: str) -> dict:
    """构造单条消息"""
    return {"role": role, "content": content}


def build_messages(system_prompt: str, history: list | None = None, user_input: str = "") -> list:
    """
    组装完整的消息列表
    :param system_prompt: 系统提示词
    :param history: 历史消息列表
    :param user_input: 用户输入
    :return: 完整消息列表
    """
    messages = []
    if system_prompt:
        messages.append(format_message("system", system_prompt))
    if history:
        messages.extend(history)
    if user_input:
        messages.append(format_message("user", user_input))
    return messages


# ====== 提问检测 ======

# 强关键词
STRONG_KEYWORDS = [
    "怎么", "怎样", "如何", "为什么", "为啥", "咋",
    "什么", "啥", "哪个", "哪些", "哪里", "哪儿", "哪种", "哪位",
    "多少", "几个", "是否", "能否", "可否",
    "能不能", "可不可以", "会不会", "有没有",
    "是不是", "对不对", "行不行",
    "多久", "多长时间", "在哪",
    "请问", "请教", "求助", "帮忙看", "帮我看",
    "谁知道", "有人知道", "有没有人知道",
    "谁能", "谁会", "有大佬", "哪位大佬",
    "想问", "问下", "问一下",
    "想知道", "告诉我",
    "报错", "error", "bug", "异常", "失败",
    "怎么解决", "怎么办", "怎么弄", "怎么搞", "怎么装",
    "怎么配", "怎么用", "怎么设置", "怎么安装",
    "教程", "文档", "示例", "example",
    "how to", "how do", "what is", "where to",
]

_FALSE_POSITIVE_PATTERNS = re.compile(
    r"(什么都[行好可]|什么的|干什么[呢啊]?$|没什么|也没什么"
    r"|说啥呢|干啥呢|啥也不是|啥都[行好]"
    r"|有没有人一起[打玩开]|有没有人想[吃玩去]"
    r"|帮我[点赞转]|帮忙[转发点赞投票砍]"
    r"|已经[修解决处理]|[修解决处理][了好完]的?"
    r"|不是bug|是feature)",
    re.IGNORECASE,
)

WEAK_SUFFIX_PARTICLES = ["吗", "嘛", "呢", "么"]

_SUFFIX_EXCLUDE_PATTERNS = re.compile(
    r"(^.{0,4}(在|正在|还在).{0,6}呢$"
    r"|^(应该|估计|大概|可能|也许).{0,8}(吗|嘛|么)$"
    r"|^(是|对|嗯|好).{0,4}(吗|嘛|么)$)"
)

WEAK_PROBLEM_KEYWORDS = [
    "不行", "不了", "不动", "不上", "不能", "无法",
    "没反应", "没效果", "没用",
    "卡住", "卡了", "卡死", "闪退", "崩溃", "白屏", "黑屏",
    "装不上", "连不上", "打不开", "用不了", "启动不了",
    "出错", "出问题", "有问题",
    "进不去", "登不上", "加载不出", "显示不了",
    "超时", "timeout", "死循环",
]

_EXCLAMATION_PATTERN = re.compile(
    r"^(哈{2,}|6{3,}|666+|牛[逼批]|nb|nice|tql|yyds|绝了|笑死|无语|离谱|可以的|厉害"
    r"|真[的]?[强牛行猛]|太[强牛猛]了|不错不错|赞|顶|支持|加油|感谢|谢谢"
    r"|收到|了解|明白|知道了|好的|ok|okk|okkk|嗯嗯|对对|是的|没错"
    r"|\+1|\+10086|doge|狗头"
    r"|我[也觉]|同感|确实|属实|有道理|说得对"
    r"|已[解决搞定修好]|搞定了|好了|可以了|没事了)[\.\.。！!~～…\s]*$",
    re.IGNORECASE,
)


def looks_like_question(text: str) -> bool:
    """判断文本是否看起来像一个提问"""
    text_lower = text.lower().strip()
    if len(text_lower) <= 2:
        return False
    if re.fullmatch(r"(\[CQ:[^\]]+\]\s*)+", text_lower):
        return False
    text_clean = re.sub(r"\[CQ:[^\]]+\]", "", text).strip()
    if _EXCLAMATION_PATTERN.fullmatch(text_clean):
        return False
    for kw in STRONG_KEYWORDS:
        if kw in text_lower:
            if _FALSE_POSITIVE_PATTERNS.search(text_clean):
                return False
    if text_lower.rstrip().endswith("?") or text_lower.rstrip().endswith("？"):
        if not re.search(r"(真的假的|不是吧|是吧|对吧|可以吧|还行吧)\s*[？?]\s*$", text_clean):
            return True
    stripped = re.sub(r"[\s。！!~～…]+$", "", text)
    for particle in WEAK_SUFFIX_PARTICLES:
        if stripped.endswith(particle):
            if _SUFFIX_EXCLUDE_PATTERNS.search(text_clean):
                return False
    if len(text_lower) <= 80:
        for kw in WEAK_PROBLEM_KEYWORDS:
            if kw in text_lower:
                return True
    return False


def extract_question_text(raw_message: str) -> str:
    """从原始消息中提取纯文本问题"""
    text = re.sub(r"\[CQ:[^\]]+\]", "", raw_message)
    text = re.sub(r"\s+", " ", text).strip()
    return text
