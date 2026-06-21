"""arkrec 插件配置"""

from dataclasses import dataclass, field


@dataclass
class ArkRecConfig:
    """全局配置"""
    email: str = ""
    password: str = ""


@dataclass
class GroupSubscription:
    """群订阅配置"""
    enabled: bool = False
    categories: list[str] = field(default_factory=list)
    operators: list[str] = field(default_factory=list)
    operations: list[str] = field(default_factory=list)
    exclude_categories: list[str] = field(default_factory=list)
