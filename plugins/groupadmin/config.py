"""群管插件配置数据结构"""

from dataclasses import dataclass, field


@dataclass
class GroupRule:
    """群审核规则"""
    group_id: str
    enabled: bool = True
    pattern: str = ""  # 正则表达式
    auto_reject: bool = False
    reject_reason: str = "回答不正确"


@dataclass
class GroupAdminConfig:
    """插件配置"""
    rules: list[GroupRule] = field(default_factory=list)
