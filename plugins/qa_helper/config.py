"""Q&A 问答插件配置"""

from dataclasses import dataclass, field


@dataclass
class QAGroupConfig:
    enabled: bool = False
    project: str = ""
    system_prompt: str = ""
