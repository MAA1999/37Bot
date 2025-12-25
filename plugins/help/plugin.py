"""帮助命令插件 - 自动解析已注册命令生成帮助信息"""

import re
from ncatbot.plugin_system import NcatBotPlugin, command_registry, param
from ncatbot.core.event import GroupMessageEvent, PrivateMessageEvent, BaseMessageEvent


class HelpPlugin(NcatBotPlugin):
    name = "HelpPlugin"
    version = "1.2.0"
    author = "Windsland52"
    dependencies = {}

    # 插件显示名称映射
    PLUGIN_NAMES = {
        "help": "帮助",
        "status": "状态",
        "mirrorchyan": "Mirror酱",
        "groupadmin": "群管",
    }

    def _get_plugin_display_name(self, plugin_name: str) -> str:
        """获取插件显示名称"""
        return self.PLUGIN_NAMES.get(plugin_name, plugin_name)

    async def _get_user_permission(self, event: BaseMessageEvent) -> str:
        """获取用户权限级别: root > admin > user"""
        user_id = str(event.user_id)

        # 检查 root
        if self.rbac_manager.user_has_role(user_id, "root"):
            return "root"

        # 检查群管理员
        if isinstance(event, GroupMessageEvent):
            try:
                info = await self.api.get_group_member_info(event.group_id, event.user_id)
                if info.role in ("owner", "admin"):
                    return "admin"
            except Exception:
                pass

        return "user"

    def _can_use_command(self, desc: str, permission: str) -> bool:
        """检查用户是否有权限使用该命令"""
        if not desc:
            return True

        # 解析权限标注
        if "[root]" in desc.lower():
            return permission == "root"
        if "[管理员]" in desc:
            return permission in ("root", "admin")

        return True

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
        permission = await self._get_user_permission(event)
        grouped = self._group_commands_by_plugin()

        # 过滤用户有权限的命令
        filtered = {}
        for plugin, cmds in grouped.items():
            visible_cmds = [
                (name, spec) for name, spec in cmds
                if self._can_use_command(spec.description, permission)
            ]
            if visible_cmds:
                filtered[plugin] = visible_cmds

        if module is None:
            # 显示模块列表
            lines = ["📚 可用模块:"]
            for plugin, cmds in sorted(filtered.items()):
                display_name = self._get_plugin_display_name(plugin)
                lines.append(f"  • {display_name} ({len(cmds)}个命令)")
            lines.append("")
            lines.append("输入 /help <模块名> 查看详细命令")
            await event.reply("\n".join(lines))
        else:
            # 查找匹配的模块
            target_plugin = None
            module_lower = module.lower()
            for plugin in filtered.keys():
                if plugin.lower() == module_lower:
                    target_plugin = plugin
                    break
                display = self._get_plugin_display_name(plugin)
                if display == module:
                    target_plugin = plugin
                    break

            if target_plugin is None:
                await event.reply(f"未找到模块: {module}")
                return

            # 显示该模块的命令
            cmds = filtered[target_plugin]
            display_name = self._get_plugin_display_name(target_plugin)
            lines = [f"📦 {display_name} 命令:"]
            for cmd_name, cmd_spec in sorted(cmds, key=lambda x: x[0]):
                desc = cmd_spec.description or "无描述"
                lines.append(f"  /{cmd_name} - {desc}")
            await event.reply("\n".join(lines))


__all__ = ["HelpPlugin"]
