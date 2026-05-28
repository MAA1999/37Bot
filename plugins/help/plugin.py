"""帮助命令插件 - 自动解析已注册命令生成帮助信息"""

from ncatbot.plugin_system import NcatBotPlugin, command_registry, param
from ncatbot.core.event import GroupMessageEvent, BaseMessageEvent


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
        "todo": "待办",
        "sensitivemonitor": "敏感词监测",
        "qahelper": "Q&A问答",
        "groupsummary": "群聊总结",
        "arkrec": "少人Wiki",
        "skland": "森空岛签到",
    }

    MODULE_ALIASES = {
        "help": "help",
        "帮助": "help",
        "status": "status",
        "状态": "status",
        "mirror": "mirrorchyan",
        "mirror酱": "mirrorchyan",
        "群管": "groupadmin",
        "groupadmin": "groupadmin",
        "todo": "todo",
        "待办": "todo",
        "敏感词": "sensitivemonitor",
        "监测": "sensitivemonitor",
        "qa": "qahelper",
        "问答": "qahelper",
        "summary": "groupsummary",
        "总结": "groupsummary",
        "arkrec": "arkrec",
        "wiki": "arkrec",
        "少人": "arkrec",
        "skland": "skland",
        "森空岛": "skland",
        "签到": "skland",
    }

    MODULE_EXAMPLES = {
        "arkrec": "/arkrec H17-3 特种\n/arkrec_exclusive 令 常规队\n/arkrec_brief 特种",
        "skland": "/skland_config add <token>\n/skland_sign\n/skland_status",
        "groupsummary": "/summary 300\n/summary_on\n/summary_status",
        "qahelper": "/qa on M9A\n/qa_status\n/qa_refresh",
        "groupadmin": "/ga_enable\n/ga_status",
        "mirrorchyan": "/mirror_sub MAA-v5.0.0\n/mirror_list",
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

    def _resolve_module(self, module: str, grouped: dict) -> str | None:
        module_lower = module.lower().strip()
        if not module_lower:
            return None
        alias_hit = self.MODULE_ALIASES.get(module_lower)
        if alias_hit and alias_hit in grouped:
            return alias_hit
        for plugin in grouped.keys():
            if plugin.lower() == module_lower:
                return plugin
            if self._get_plugin_display_name(plugin) == module:
                return plugin
        return None

    def _search_commands(self, keyword: str, grouped: dict) -> list[tuple[str, str, str]]:
        kw = keyword.lower().strip()
        if not kw:
            return []
        hits = []
        for plugin, cmds in grouped.items():
            display = self._get_plugin_display_name(plugin)
            for cmd_name, cmd_spec in cmds:
                desc = cmd_spec.description or ""
                if kw in cmd_name.lower() or kw in desc.lower() or kw in display.lower():
                    hits.append((cmd_name, desc, display))
        hits.sort(key=lambda item: (item[2], item[0]))
        return hits

    @command_registry.command("help", description="帮助: /help [模块名|关键词]")
    @param(name="module", default="", help="模块名称")
    async def help_cmd(self, event: BaseMessageEvent, module: str = ""):
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

        if module.strip() == "":
            lines = ["📚 可用模块"]
            for plugin, cmds in sorted(
                filtered.items(),
                key=lambda item: self._get_plugin_display_name(item[0])
            ):
                display_name = self._get_plugin_display_name(plugin)
                lines.append(f"• {display_name} ({len(cmds)} 个命令)  /help {plugin}")
            lines.append("")
            lines.append("用法：")
            lines.append("• /help <模块名>  查看模块命令")
            lines.append("• /help <关键词>  搜索命令（如 arkrec、summary、订阅）")
            await event.reply("\n".join(lines))
            return

        target_plugin = self._resolve_module(module, filtered)
        if target_plugin is not None:
            cmds = filtered[target_plugin]
            display_name = self._get_plugin_display_name(target_plugin)
            lines = [f"📦 {display_name}"]
            for cmd_name, cmd_spec in sorted(cmds, key=lambda x: x[0]):
                desc = cmd_spec.description or "无描述"
                lines.append(f"/{cmd_name} - {desc}")
            example = self.MODULE_EXAMPLES.get(target_plugin)
            if example:
                lines.append("")
                lines.append("示例：")
                lines.append(example)
            await event.reply("\n".join(lines))
            return

        hits = self._search_commands(module, filtered)
        if not hits:
            await event.reply(f"未找到模块或命令: {module}\n可先用 /help 查看模块列表")
            return
        lines = [f"🔎 搜索结果: {module}"]
        for cmd_name, desc, plugin_display in hits[:12]:
            lines.append(f"/{cmd_name} - {desc} [{plugin_display}]")
        if len(hits) > 12:
            lines.append(f"... 其余 {len(hits) - 12} 条请换更精确关键词")
        await event.reply("\n".join(lines))


__all__ = ["HelpPlugin"]
