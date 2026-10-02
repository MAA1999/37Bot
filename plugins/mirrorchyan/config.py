"""配置数据结构"""

from dataclasses import dataclass, field

# 资源类型 -> (os, arch, 显示名, 后缀说明)
# os/arch 就是发给 Mirror API 的查询参数，None 表示不带该参数（通用包）。
#
# 同一个软件的「主 rid」和「_exec rid」常常是不同平台，平台不能从资源名推：
#   M9A       = 桌面(win/linux)   M9A_exec     = 安卓
#   MaaEnd    = 桌面(win/linux)   MaaEnd_exec  = macOS + 安卓(仅 beta/alpha)
# 所以平台必须由调用方按类型显式指定。
#
# 这里不记文件后缀：同一个平台后缀并不固定（linux 是 .tar.gz，macos 是 .dmg，
# windows 是 .zip），一律从下载 URL 推导，见 api.probe_ext。
RESOURCE_TYPES = {
    0: (None, None, "通用", ""),
    1: ("windows", "x64", "跨平台", ""),
    2: ("android", "any", "安卓通用", "，下载后请改回 .apk 再安装"),
    3: ("android", "arm64", "安卓arm64", "，下载后请改回 .apk 再安装"),
    4: ("android", "x64", "安卓x86_64", "，下载后请改回 .apk 再安装"),
    5: ("macos", "arm64", "macOS-arm64", ""),
    6: ("macos", "x64", "macOS-x86_64", ""),
    7: ("linux", "x64", "Linux-x86_64", ""),
    8: ("linux", "arm64", "Linux-arm64", ""),
    9: ("windows", "arm64", "Windows-arm64", ""),
}

# 类型参数的帮助文本，命令声明与校验失败提示共用
TYPE_HINT = (
    "资源类型 0通用/1跨平台/2安卓通用/3安卓arm64/4安卓x86_64"
    "/5macOS-arm64/6macOS-x86_64/7Linux-x86_64/8Linux-arm64/9Windows-arm64"
)


def resolve_type(resource_type) -> tuple:
    """把资源类型解析成 (os, arch, 显示名, 后缀说明)"""
    try:
        return RESOURCE_TYPES[int(resource_type)]
    except (KeyError, TypeError, ValueError):
        return RESOURCE_TYPES[0]


@dataclass
class ResourceConfig:
    """单个资源的配置"""

    rid: str  # 资源ID，如 M9A
    type: int  # 见 RESOURCE_TYPES
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
