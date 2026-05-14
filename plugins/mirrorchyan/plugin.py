"""MirrorChyan 软件更新检测插件"""

import asyncio
import re
import json
from pathlib import Path
from dataclasses import asdict

from ncatbot.plugin_system import NcatBotPlugin, command_registry, param
from ncatbot.core.event import GroupMessageEvent, PrivateMessageEvent
from ncatbot.utils import get_log

from .config import MirrorConfig, GroupSubscription, ResourceConfig
from .api import get_latest_version, download_resource

logger = get_log("MirrorChyan")


class MirrorChyanPlugin(NcatBotPlugin):
    name = "MirrorChyanPlugin"
    version = "1.0.0"
    author = "Windsland52"
    dependencies = {}

    async def on_load(self):
        """插件加载"""
        # 使用框架提供的 workspace 目录
        self.data_dir = self.workspace
        self.config_path = self.data_dir / "config.json"
        self.state_path = self.data_dir / "state.json"

        self.mirror_config = self._load_config()
        self.state = self._load_state()  # {rid: last_version}

        # 启动定时检查
        self._start_check_tasks()

    async def _is_group_admin(self, group_id: str, user_id: str) -> bool:
        """检查用户是否是群主或管理员"""
        try:
            info = await self.api.get_group_member_info(group_id, user_id)
            role = info.role
            logger.info(f"group={group_id}, user={user_id}, role={role}")
            return role in ("owner", "admin")
        except Exception as e:
            logger.error(f"get_group_member_info error: {e}")
            return False

    def _start_check_tasks(self):
        """启动所有订阅的定时检查任务"""
        for sub in self.mirror_config.subscriptions:
            for res in sub.resources:
                task_name = (
                    f"mirror_{sub.group_id}_{res.rid}_{res.type}"
                )
                self.add_scheduled_task(
                    self._make_check_task(sub.group_id, res),
                    task_name,
                    f"{res.interval}s",
                )

    def _make_check_task(self, group_id: str, res: ResourceConfig):
        """创建检查任务闭包"""

        async def task():
            await self._check_resource(group_id, res)

        return task

    # ========== 配置管理 ==========

    def _load_config(self) -> MirrorConfig:
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                return self._dict_to_config(data)
            except Exception:
                pass
        return MirrorConfig()

    def _save_config(self):
        data = self._config_to_dict(self.mirror_config)
        self.config_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_state(self):
        self.state_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _dict_to_config(self, data: dict) -> MirrorConfig:
        subs = []
        for s in data.get("subscriptions", []):
            resources = [ResourceConfig(**r) for r in s.get("resources", [])]
            subs.append(GroupSubscription(group_id=s["group_id"], resources=resources))
        return MirrorConfig(
            subscriptions=subs,
            cdk=data.get("cdk", ""),
        )

    def _config_to_dict(self, cfg: MirrorConfig) -> dict:
        return {
            "subscriptions": [
                {"group_id": s.group_id, "resources": [asdict(r) for r in s.resources]}
                for s in cfg.subscriptions
            ],
            "cdk": cfg.cdk,
        }

    # ========== 定时检查 ==========

    async def _check_resource(self, group_id: str, res: ResourceConfig):
        """检查单个资源更新"""
        data = await get_latest_version(res.rid, res.type, res.channel)
        if not data:
            return

        version = data.get("version_name", "")
        state_key = f"{res.rid}_{res.type}_{res.channel}"
        last_version = self.state.get(state_key, "")

        if version and version != last_version:
            self.state[state_key] = version
            self._save_state()
            await self._notify_update(group_id, res, data)

            # 自动上传
            if res.auto and self.mirror_config.cdk:
                await self._auto_upload(group_id, res, data)

    async def _check_resource_force(self, group_id: str, res: ResourceConfig):
        """强制获取并显示更新信息"""
        data = await get_latest_version(res.rid, res.type, res.channel)
        if not data:
            return
        await self._notify_update(group_id, res, data)

    def _parse_release_note(self, note: str) -> str:
        """解析并格式化更新说明"""
        if not note:
            return ""

        # 移除 HTML 注释
        note = re.sub(r'<!--.*?-->', '', note, flags=re.DOTALL)
        # 移除链接但保留文字
        note = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', note)
        # 移除图片
        note = re.sub(r'!\[.*?\]\(.*?\)', '', note)
        # 移除引用块标记
        note = re.sub(r'^>\s*', '', note, flags=re.MULTILINE)

        sections = {}
        current_section = None
        current_items = []

        for line in note.split('\n'):
            line = line.strip()
            if not line:
                continue

            # 检测分类标题 (### 🐛 Bug修复)
            if line.startswith('#'):
                if current_section and current_items:
                    sections[current_section] = current_items
                # 提取标题文字
                title = re.sub(r'^#+\s*', '', line)
                current_section = title
                current_items = []
            # 检测列表项 (- xxx 或 * xxx)
            elif line.startswith(('-', '*')) and current_section:
                item = re.sub(r'^[-*]\s*', '', line)
                # 清理粗体/斜体
                item = re.sub(r'\*+([^*]+)\*+', r'\1', item)
                if item:
                    current_items.append(item)

        if current_section and current_items:
            sections[current_section] = current_items

        # 格式化输出
        result = []
        for title, items in sections.items():
            if items:
                result.append(title)
                for item in items:
                    result.append(f"  • {item}")

        return '\n'.join(result) if result else "无详细说明"

    async def _notify_update(self, group_id: str, res: ResourceConfig, data: dict):
        """发送更新通知"""
        version = data.get('version_name', '')
        release_note = self._parse_release_note(data.get('release_note', ''))

        msg = (
            f"📦 {res.rid} 更新 {version}\n"
            f"━━━━━━━━━━━━━━\n"
            f"{release_note}"
        )
        await self.api.post_group_msg(group_id, text=msg)

    async def _get_or_create_folder(self, group_id: str, folder_name: str) -> tuple[str, str]:
        """获取或创建文件夹，返回 (文件夹ID, 错误信息)"""
        try:
            root_files = await self.api.get_group_root_files(group_id)
        except Exception as e:
            return "", f"获取文件列表失败: {e}"

        folders = root_files.get("folders") or []

        # 查找已存在的文件夹
        for folder in folders:
            if folder.get("folder_name") == folder_name:
                return folder.get("folder_id", ""), ""

        # 不存在则创建
        try:
            await self.api.create_group_file_folder(group_id, folder_name)
        except Exception as e:
            return "", f"创建文件夹失败: {e}"

        # 重新获取文件夹ID
        try:
            root_files = await self.api.get_group_root_files(group_id)
            folders = root_files.get("folders") or []
            for folder in folders:
                if folder.get("folder_name") == folder_name:
                    return folder.get("folder_id", ""), ""
        except Exception:
            pass

        return "", "创建文件夹失败(可能需要管理员权限)"

    async def _file_exists_in_folder(self, group_id: str, folder_id: str, filename: str) -> bool:
        """检查文件夹中是否已存在同名文件"""
        try:
            if folder_id:
                files_data = await self.api.get_group_files_by_folder(group_id, folder_id)
            else:
                files_data = await self.api.get_group_root_files(group_id)
            files = files_data.get("files", [])
            for f in files:
                if f.get("file_name") == filename:
                    return True
        except Exception as e:
            logger.error(f"检查文件是否存在失败: {e}")
        return False

    async def _auto_upload(self, group_id: str, res: ResourceConfig, data: dict):
        """自动下载并上传到群文件"""
        type_name = "通用" if res.type == 0 else "win-x64"
        filename = f"{res.rid}-{type_name}.zip"
        save_path = str((self.data_dir / filename).resolve())

        ok, err, _ = await download_resource(
            res.rid, res.type, res.channel, self.mirror_config.cdk, save_path
        )

        if not ok:
            await self.api.post_group_msg(group_id, text=f"自动下载失败: {err}")
            return

        try:
            version = data.get("version_name", "")
            upload_name = f"{res.rid}-{type_name}-{version}.zip"
            folder_id, folder_err = await self._get_or_create_folder(group_id, f"{res.rid}下载")

            if folder_err:
                await self.api.post_group_msg(group_id, text=f"{folder_err}，上传到根目录")

            # 检查是否已存在同名文件
            if await self._file_exists_in_folder(group_id, folder_id, upload_name):
                await self.api.post_group_msg(group_id, text=f"群文件已存在: {upload_name}，跳过上传")
                return

            try:
                await self.api.upload_group_file(group_id, save_path, upload_name, folder=folder_id)
            except Exception as ue:
                if type(ue).__name__ == "Empty":
                    # QQ 上传是异步的，返回空不代表失败，等一等再确认
                    for delay in (10, 20, 30):
                        await asyncio.sleep(delay)
                        if await self._file_exists_in_folder(group_id, folder_id, upload_name):
                            await self.api.post_group_msg(group_id, text=f"自动上传成功: {upload_name}")
                            return
                    await self.api.post_group_msg(group_id, text=f"自动上传中: {upload_name}，请稍后检查群文件")
                    return
                raise
            await self.api.post_group_msg(group_id, text=f"自动上传成功: {upload_name}")
        except Exception as e:
            err_msg = str(e) or repr(e)
            await self.api.post_group_msg(group_id, text=f"自动上传失败: {type(e).__name__}: {err_msg}")

    # ========== 群聊命令 ==========

    def _get_group_sub(self, group_id: str) -> GroupSubscription:
        """获取或创建群订阅"""
        for sub in self.mirror_config.subscriptions:
            if sub.group_id == group_id:
                return sub
        sub = GroupSubscription(group_id=group_id)
        self.mirror_config.subscriptions.append(sub)
        return sub

    @command_registry.command("mirror_sub", description="[管理员] 订阅资源")
    @param(name="type", default=1, help="类型 0通用/1跨平台")
    @param(name="channel", default="stable", help="渠道 stable/beta/alpha")
    @param(name="interval", default=600, help="检查间隔(秒)")
    @param(name="auto", default=False, help="自动上传")
    async def cmd_sub(
        self,
        event: GroupMessageEvent,
        rid: str,
        type: int = 1,
        channel: str = "stable",
        interval: int = 600,
        auto: bool = False,
    ):
        """订阅资源"""
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要管理员权限")
            return

        # 参数验证
        if type not in (0, 1):
            await event.reply("类型只能是 0(通用) 或 1(跨平台)")
            return
        if channel not in ("stable", "beta", "alpha"):
            await event.reply("渠道只能是 stable/beta/alpha")
            return
        if interval < 60:
            await event.reply("检查间隔至少60秒")
            return

        group_id = str(event.group_id)
        sub = self._get_group_sub(group_id)

        # 检查是否已订阅
        for r in sub.resources:
            if r.rid == rid and r.type == type:
                await event.reply(f"已订阅 {rid}")
                return

        res = ResourceConfig(
            rid=rid,
            type=type,
            channel=channel,
            interval=interval,
            auto=auto,
        )
        sub.resources.append(res)
        self._save_config()

        # 启动定时任务
        task_name = f"mirror_{group_id}_{res.rid}_{res.type}"
        self.add_scheduled_task(
            self._make_check_task(group_id, res),
            task_name,
            f"{res.interval}s",
        )

        type_name = "通用" if type == 0 else "跨平台"
        auto_str = "是" if auto else "否"
        await event.reply(
            f"订阅成功: {rid} ({type_name}, {channel}, {interval}s, 自动上传:{auto_str})"
        )

    @command_registry.command("mirror_unsub", description="[管理员] 取消订阅")
    @param(name="type", default=1, help="类型 0通用/1跨平台")
    async def cmd_unsub(
        self, event: GroupMessageEvent, rid: str, type: int = 0
    ):
        """取消订阅"""
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要管理员权限")
            return
        group_id = str(event.group_id)
        for sub in self.mirror_config.subscriptions:
            if sub.group_id == group_id:
                for r in sub.resources[:]:
                    if r.rid == rid and r.type == type:
                        sub.resources.remove(r)
                        self._save_config()
                        # 停止定时任务
                        task_name = f"mirror_{group_id}_{rid}_{type}"
                        self.remove_scheduled_task(task_name)
                        await event.reply(f"已取消订阅: {rid}")
                        return
        await event.reply(f"未找到订阅: {rid}")

    @command_registry.command("mirror_list", description="查看本群订阅")
    async def cmd_list(self, event: GroupMessageEvent):
        """查看订阅列表"""
        group_id = str(event.group_id)
        for sub in self.mirror_config.subscriptions:
            if sub.group_id == group_id and sub.resources:
                lines = ["本群订阅:"]
                for r in sub.resources:
                    t = "通用" if r.type == 0 else "跨平台"
                    lines.append(f"  {r.rid} ({t}, {r.channel})")
                await event.reply("\n".join(lines))
                return
        await event.reply("本群暂无订阅")

    @command_registry.command("mirror_check", description="[管理员] 立即检查更新")
    @param(name="rid", default=None, help="资源ID，不填则检查全部")
    @param(name="force", default=False, help="强制显示更新信息")
    async def cmd_check(self, event: GroupMessageEvent, rid: str = None, force: bool = False):
        """手动检查更新"""
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要管理员权限")
            return

        group_id = str(event.group_id)
        for sub in self.mirror_config.subscriptions:
            if sub.group_id == group_id:
                checked = 0
                for r in sub.resources:
                    if rid is None or r.rid == rid:
                        if force:
                            await self._check_resource_force(group_id, r)
                        else:
                            await self._check_resource(group_id, r)
                        checked += 1
                if checked > 0:
                    await event.reply(f"已检查 {checked} 个资源")
                else:
                    await event.reply(f"未找到资源: {rid}")
                return
        await event.reply("本群暂无订阅")

    @command_registry.command("mirror_config", description="[管理员] 修改订阅配置")
    @param(name="type", default=0, help="资源类型 0通用/1跨平台")
    @param(name="interval", default=None, help="检查间隔(秒)")
    @param(name="auto", default=None, help="自动上传 true/false")
    @param(name="channel", default=None, help="渠道 stable/beta/alpha")
    async def cmd_config(
        self,
        event: GroupMessageEvent,
        rid: str,
        type: int = 1,
        interval: int = None,
        auto: bool = None,
        channel: str = None,
    ):
        """更新配置 用法: /mirror_config <资源ID> [类型0/1] [检查间隔秒] [自动上传]"""
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要管理员权限")
            return

        group_id = str(event.group_id)
        for sub in self.mirror_config.subscriptions:
            if sub.group_id == group_id:
                for r in sub.resources:
                    if r.rid == rid and r.type == type:
                        updated = []
                        if interval is not None:
                            r.interval = interval
                            updated.append(f"检查间隔={interval}s")
                            # 重新注册定时任务
                            task_name = (
                                f"mirror_{group_id}_{r.rid}_{r.type}"
                            )
                            self.add_scheduled_task(
                                self._make_check_task(group_id, r),
                                task_name,
                                f"{r.interval}s",
                            )
                        if auto is not None:
                            r.auto = auto
                            updated.append(f"自动上传={'是' if auto else '否'}")
                        if channel is not None:
                            if channel not in ("stable", "beta", "alpha"):
                                await event.reply("渠道只能是 stable/beta/alpha")
                                return
                            r.channel = channel
                            updated.append(f"渠道={channel}")
                        if updated:
                            self._save_config()
                            await event.reply(f"配置已更新: {', '.join(updated)}")
                        else:
                            await event.reply("未指定要更新的配置")
                        return
        await event.reply(f"未找到订阅: {rid}")

    @command_registry.command("mirror_download", description="[管理员] 下载资源到群文件")
    @param(name="type", default=1, help="类型 0通用/1跨平台")
    @param(name="channel", default="stable", help="渠道 stable/beta/alpha")
    async def cmd_download(
        self,
        event: GroupMessageEvent,
        rid: str,
        type: int = 1,
        channel: str = "stable",
    ):
        """下载并上传"""
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要管理员权限")
            return

        # 参数验证
        if type not in (0, 1):
            await event.reply("类型只能是 0(通用) 或 1(跨平台)")
            return
        if channel not in ("stable", "beta", "alpha"):
            await event.reply("渠道只能是 stable/beta/alpha")
            return

        if not self.mirror_config.cdk:
            await event.reply("未设置CDK，请管理员私聊设置")
            return

        await event.reply(f"开始下载 {rid}...")

        # 下载文件
        type_name = "通用" if type == 0 else "win-x64"
        filename = f"{rid}-{type_name}.zip"
        save_path = str((self.data_dir / filename).resolve())

        ok, msg, data = await download_resource(
            rid, type, channel, self.mirror_config.cdk, save_path
        )

        if not ok:
            await event.reply(f"下载失败: {msg}")
            return

        # 提示跳过下载或下载完成
        if msg:
            await event.reply(msg)

        # 上传到群文件
        try:
            version = data.get("version_name", "")
            upload_name = f"{rid}-{type_name}-{version}.zip"
            folder_id, folder_err = await self._get_or_create_folder(str(event.group_id), f"{rid}下载")

            if folder_err:
                await event.reply(f"{folder_err}，上传到根目录")

            # 检查是否已存在同名文件
            if await self._file_exists_in_folder(str(event.group_id), folder_id, upload_name):
                await event.reply(f"群文件已存在: {upload_name}，跳过上传")
                return

            await self.api.upload_group_file(event.group_id, save_path, upload_name, folder=folder_id)
            await event.reply(f"上传成功: {upload_name}")
        except Exception as e:
            await event.reply(f"上传失败: {e}")

    # ========== 私聊命令 ==========

    @command_registry.command("mirror_cdk", description="[root] 设置CDK密钥(私聊)")
    async def cmd_cdk(self, event: PrivateMessageEvent, cdk: str):
        """设置CDK"""
        # 只允许私聊
        if event.message_type != "private":
            await event.reply("请私聊设置CDK")
            return
        # 只允许 root
        if not self.rbac_manager.user_has_role(str(event.user_id), "root"):
            await event.reply("需要root权限")
            return
        self.mirror_config.cdk = cdk
        self._save_config()
        await event.reply("CDK 设置成功")


__all__ = ["MirrorChyanPlugin"]
