"""敏感消息监听插件配置"""

from dataclasses import dataclass, field


@dataclass
class SensitiveGroupConfig:
    enabled: bool = False
    notify_users: list[str] = field(default_factory=list)
    warn_in_group: bool = False
