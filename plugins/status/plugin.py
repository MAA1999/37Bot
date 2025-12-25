"""服务器状态查询插件"""

import time
import psutil
from ncatbot.plugin_system import NcatBotPlugin, command_registry
from ncatbot.core.event import BaseMessageEvent


class StatusPlugin(NcatBotPlugin):
    name = "StatusPlugin"
    version = "1.0.0"
    author = "Windsland52"
    dependencies = {}

    @command_registry.command("status", description="查询服务器状态")
    async def status_cmd(self, event: BaseMessageEvent):
        """查询服务器 CPU、内存、磁盘使用率和运行时间"""
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage("/")

        uptime_seconds = time.time() - psutil.boot_time()
        days, remainder = divmod(int(uptime_seconds), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)

        status_text = (
            f"CPU: {cpu}%\n"
            f"内存: {mem.percent}% ({mem.used // 1024 // 1024}MB / {mem.total // 1024 // 1024}MB)\n"
            f"Swap: {swap.percent}% ({swap.used // 1024 // 1024}MB / {swap.total // 1024 // 1024}MB)\n"
            f"磁盘: {disk.percent}% ({disk.used // 1024 // 1024 // 1024}GB / {disk.total // 1024 // 1024 // 1024}GB)\n"
            f"运行时间: {days}天 {hours}小时 {minutes}分钟"
        )
        await event.reply(status_text)


__all__ = ["StatusPlugin"]
