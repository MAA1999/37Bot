"""Q&A 问答插件配置"""

from dataclasses import dataclass, field


@dataclass
class QAGroupConfig:
    enabled: bool = False
    projects: list[str] = field(default_factory=list)
    system_prompt: str = ""
