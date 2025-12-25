"""帮助命令插件 - 自动解析已注册命令生成帮助信息"""

from ncatbot.plugin_system import NcatBotPlugin, command_registry
from ncatbot.core.event import BaseMessageEvent


class HelpPlugin(NcatBotPlugin):
    name = "HelpPlugin"
    version = "1.0.0"
    author = "Windsland52"
    dependencies = {}

    @command_registry.command("help", description="显示所有可用命令")
    async def help_cmd(self, event: BaseMessageEvent):
        """显示所有已注册命令的帮助信息"""
        commands = command_registry.get_all_commands()

        lines = ["可用命令:"]
        for name, cmd_spec in sorted(commands.items()):
            cmd_name = name[0] if isinstance(name, tuple) else name
            desc = cmd_spec.description or "无描述"
            lines.append(f"/{cmd_name} - {desc}")

        await event.reply("\n".join(lines))


__all__ = ["HelpPlugin"]
