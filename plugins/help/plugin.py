"""帮助命令插件 - 自动解析已注册命令生成帮助信息"""

from ncatbot.plugin_system import NcatBotPlugin, command_registry, param
from ncatbot.core.event import GroupMessageEvent, PrivateMessageEvent, BaseMessageEvent


class HelpPlugin(NcatBotPlugin):
    name = "HelpPlugin"
    version = "1.1.0"
    author = "Windsland52"
    dependencies = {}

    # 插件显示名称映射
    PLUGIN_NAMES = {
        "help": "帮助",
        "status": "状态",
        "mirrorchyan": "Mirror酱",
    }

    def _get_plugin_display_name(self, plugin_name: str) -> str:
        """获取插件显示名称"""
        return self.PLUGIN_NAMES.get(plugin_name, plugin_name)

    def _group_commands_by_plugin(self) -> dict:
        """按插件分组命令"""
        commands = command_registry.get_all_commands()
        grouped = {}
        for name, cmd_spec in commands.items():
            plugin = cmd_spec.plugin_name or "其他"
            if plugin not in grouped:
                grouped[plugin] = []
            cmd_name = name[0] if isinstance(name, tuple) else name
            grouped[plugin].append((cmd_name, cmd_spec))
        return grouped

    @command_registry.command("help", description="显示帮助信息")
    @param(name="module", default=None, help="模块名称")
    async def help_cmd(self, event: BaseMessageEvent, module: str = None):
        """显示帮助信息"""
        grouped = self._group_commands_by_plugin()

        if module is None:
            # 显示模块列表
            lines = ["📚 可用模块:"]
            for plugin, cmds in sorted(grouped.items()):
                display_name = self._get_plugin_display_name(plugin)
                lines.append(f"  • {display_name} ({len(cmds)}个命令)")
            lines.append("")
            lines.append("输入 /help <模块名> 查看详细命令")
            await event.reply("\n".join(lines))
        else:
            # 查找匹配的模块
            target_plugin = None
            module_lower = module.lower()
            for plugin in grouped.keys():
                if plugin.lower() == module_lower:
                    target_plugin = plugin
                    break
                # 也支持用显示名称查找
                display = self._get_plugin_display_name(plugin)
                if display == module:
                    target_plugin = plugin
                    break

            if target_plugin is None:
                await event.reply(f"未找到模块: {module}")
                return

            # 显示该模块的命令
            cmds = grouped[target_plugin]
            display_name = self._get_plugin_display_name(target_plugin)
            lines = [f"📦 {display_name} 命令:"]
            for cmd_name, cmd_spec in sorted(cmds, key=lambda x: x[0]):
                desc = cmd_spec.description or "无描述"
                lines.append(f"  /{cmd_name} - {desc}")
            await event.reply("\n".join(lines))


__all__ = ["HelpPlugin"]
