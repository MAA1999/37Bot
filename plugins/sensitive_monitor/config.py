"""敏感消息监听插件配置"""

from dataclasses import dataclass, field


@dataclass
class SensitiveGroupConfig:
    enabled: bool = False
    notify_users: list[str] = field(default_factory=list)
    warn_in_group: bool = False
    # Whether to append recent conversation context to notifications
    append_context: bool = False
    # Maximum number of recent messages to include when appending context
    max_context_messages: int = 5
    # Maximum total characters for the appended context
    max_context_chars: int = 800
    # Cooldown seconds: after a sensitive notification, suppress further
    # notifications from this group for this duration to avoid cascade
    notify_cooldown_seconds: int = 120
