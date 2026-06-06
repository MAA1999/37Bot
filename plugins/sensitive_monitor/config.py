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
    review_mode: str = "balanced"
    min_confidence: float = 0.85
    notify_cooldown: int = 300
    context_for_judge: bool = False
