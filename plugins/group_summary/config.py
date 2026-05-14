"""群聊总结插件配置"""

from dataclasses import dataclass, field


@dataclass
class SummaryGroupConfig:
    enabled: bool = False
    auto: bool = False
    auto_hour: int = 22
    message_count: int = 200
    track_issues: bool = True
    last_summary_date: str = ""
