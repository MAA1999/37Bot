"""配置数据结构"""

from dataclasses import dataclass, field


@dataclass
class ResourceConfig:
    """单个资源的配置"""

    rid: str  # 资源ID，如 M9A
    type: int  # 0=通用, 1=跨平台
    channel: str = "stable"  # stable | beta | alpha
    interval: int = 600  # 检查间隔(秒)，默认10分钟
    auto: bool = False  # 是否自动上传群文件


@dataclass
class GroupSubscription:
    """群订阅配置"""

    group_id: str
    resources: list[ResourceConfig] = field(default_factory=list)


@dataclass
class MirrorConfig:
    """插件配置"""

    subscriptions: list[GroupSubscription] = field(default_factory=list)
    cdk: str = ""
