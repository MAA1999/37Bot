"""森空岛签到插件。"""

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

from ncatbot.core.event import BaseMessageEvent, GroupMessageEvent, PrivateMessageEvent
from ncatbot.plugin_system import NcatBotPlugin, command_registry, param
from ncatbot.utils import get_log

from .api import SignResult, SklandAuthError, SklandClient

logger = get_log("Skland")

SMS_SESSION_TTL = 300


@dataclass
class SklandAccount:
    name: str
    token: str
    device_id: str = ""
    owner_user_id: str = ""
    notify_type: str = "private"
    notify_id: str = ""
    auth_failed_notified: bool = False
    last_error: str = ""
    last_success_at: str = ""


@dataclass
class SklandConfig:
    enabled: bool = True
    hour: int = 0
    minute: int = 1
    accounts: list[SklandAccount] = field(default_factory=list)
    last_run_date: str = ""


class SklandPlugin(NcatBotPlugin):
    name = "SklandPlugin"
    version = "1.0.0"
    author = "Windsland52"
    dependencies = {}

    async def on_load(self):
        self.config_path = self.workspace / "config.json"
        self.cfg = self._load_config()
        self._lock = asyncio.Lock()
        self._sms_sessions: dict[str, dict] = {}
        self.add_scheduled_task(self._daily_tick, "skland_daily_tick", "60s")
        logger.info("Skland scheduled task registered")

    def _load_config(self) -> SklandConfig:
        if not self.config_path.exists():
            return SklandConfig()
        try:
            raw = json.loads(self.config_path.read_text("utf-8"))
            accounts = [SklandAccount(**item) for item in raw.get("accounts", [])]
            return SklandConfig(
                enabled=bool(raw.get("enabled", True)),
                hour=int(raw.get("hour", 0)),
                minute=int(raw.get("minute", 1)),
                accounts=accounts,
                last_run_date=str(raw.get("last_run_date", "")),
            )
        except Exception as e:
            logger.error(f"读取森空岛配置失败: {e}")
            return SklandConfig()

    def _save_config(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(asdict(self.cfg), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _is_root(self, event: BaseMessageEvent) -> bool:
        return self.rbac_manager.user_has_role(str(event.user_id), "root")

    def _can_manage_account(self, event: BaseMessageEvent, account_name: str) -> bool:
        """账号 owner 或 root 可以管理该账号。"""
        if self._is_root(event):
            return True
        uid = str(event.user_id)
        for a in self.cfg.accounts:
            if a.name == account_name:
                return a.owner_user_id == uid
        return False

    def _token_fingerprint(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]

    def _now_text(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _event_notify_target(self, event: BaseMessageEvent) -> tuple[str, str]:
        if isinstance(event, GroupMessageEvent):
            return "group", str(event.group_id)
        return "private", str(event.user_id)

    async def _build_account_from_token(
        self,
        token: str,
        owner_user_id: str,
        notify_type: str,
        notify_id: str,
    ) -> SklandAccount:
        client = SklandClient()
        token_fp = self._token_fingerprint(token)
        try:
            logger.info(f"skland add start owner={owner_user_id} token_fp={token_fp} did={client.device_id}")
            grant_code = await client.get_grant_code(token)
            logger.info(f"skland add grant ok owner={owner_user_id} token_fp={token_fp}")
            cred = await client.get_credential(grant_code)
            logger.info(f"skland add cred ok owner={owner_user_id} token_fp={token_fp}")
            bindings = await client.get_binding_list(cred)
            logger.info(f"skland add binding ok owner={owner_user_id} count={len(bindings)}")
            if not bindings:
                raise RuntimeError("未找到绑定角色，无法生成账号 uid")
            arknights = next((item for item in bindings if item.app_code == "arknights"), None)
            uid = (arknights or bindings[0]).uid
            if not uid:
                uid = f"{(arknights or bindings[0]).app_code}_{(arknights or bindings[0]).nickname}"
            return SklandAccount(
                name=uid,
                token=token,
                device_id=client.device_id,
                owner_user_id=owner_user_id,
                notify_type=notify_type,
                notify_id=notify_id,
            )
        finally:
            await client.close()

    async def _add_account_from_token(
        self,
        token: str,
        owner_user_id: str,
        notify_type: str,
        notify_id: str,
    ) -> tuple[SklandAccount, str]:
        account = await self._build_account_from_token(
            token,
            owner_user_id,
            notify_type,
            notify_id,
        )
        self.cfg.accounts = [a for a in self.cfg.accounts if a.name != account.name]
        self.cfg.accounts.append(account)
        self._save_config()
        logger.info(
            f"skland account saved uid={account.name} owner={account.owner_user_id} "
            f"notify_type={account.notify_type} notify_id={account.notify_id}"
        )
        sign_text = await self._run_sign(account.name)
        return account, sign_text

    def _format_results(self, title: str, lines: list[str]) -> str:
        if not lines:
            lines = ["没有可签到的明日方舟角色"]
        return title + "\n" + "\n".join(lines)

    def _format_account_result(
        self,
        account: SklandAccount,
        results: list[SignResult] | None = None,
        error: str = "",
    ) -> list[str]:
        lines = [f"[{account.name}]"]
        if error:
            lines.append(f"失败: {error}")
            return lines
        assert results is not None
        if not results:
            lines.append("未找到绑定角色")
            return lines
        for item in results:
            role = f"{item.game_name} {item.nickname}({item.channel_name})"
            if item.success:
                awards = "、".join(item.awards) if item.awards else "无奖励明细"
                lines.append(f"{role}: 成功，{awards}")
            elif item.already_signed:
                lines.append(f"{role}: 今日已签到")
            else:
                lines.append(f"{role}: 失败，{item.error}")
        return lines

    async def _notify_auth_failed_once(self, account: SklandAccount, error: str):
        if account.auth_failed_notified:
            logger.info(f"skland auth failure already notified uid={account.name}")
            return
        account.auth_failed_notified = True
        account.last_error = error
        self._save_config()

        owner = account.owner_user_id
        text = f"森空岛账号 {account.name} 的 token 可能已过期，自动刷新/换取 cred 失败，请更新 token。错误: {error}"
        logger.warning(f"skland auth failed notify uid={account.name} owner={owner} error={error}")
        if account.notify_type == "group" and account.notify_id:
            try:
                prefix = f"[CQ:at,qq={owner}] " if owner else ""
                await self.api.post_group_msg(account.notify_id, text=prefix + text)
            except Exception as e:
                logger.error(f"森空岛 token 失效群提醒失败 uid={account.name} group={account.notify_id}: {e}")
            return

        target = account.notify_id or owner
        if target:
            try:
                await self.api.post_private_msg(target, text=text)
            except Exception as e:
                logger.error(f"森空岛 token 失效私聊提醒失败 uid={account.name} user={target}: {e}")

    async def _send_account_result(self, account: SklandAccount, text: str):
        if account.notify_type == "group" and account.notify_id:
            try:
                await self.api.post_group_msg(account.notify_id, text=text[:2000])
            except Exception as e:
                logger.error(f"发送森空岛签到结果到群失败 uid={account.name} group={account.notify_id}: {e}")
            return

        target = account.notify_id or account.owner_user_id
        if target:
            try:
                await self.api.post_private_msg(target, text=text[:2000])
            except Exception as e:
                logger.error(f"发送森空岛签到结果到私聊失败 uid={account.name} user={target}: {e}")

    async def _run_one_text(self, account: SklandAccount) -> str:
        lines = await self._run_one(account)
        return self._format_results("森空岛签到", lines)

    async def _run_daily_sign(self):
        for account in self.cfg.accounts:
            text = await self._run_one_text(account)
            await self._send_account_result(account, text)
            logger.info(
                f"skland daily account sent uid={account.name} "
                f"notify_type={account.notify_type} notify_id={account.notify_id}"
            )

    async def _run_one(self, account: SklandAccount) -> list[str]:
        client = SklandClient(account.device_id or None)
        if not account.device_id:
            account.device_id = client.device_id
            self._save_config()
        started = datetime.now()
        token_fp = self._token_fingerprint(account.token)
        logger.info(
            f"skland sign start uid={account.name} owner={account.owner_user_id} "
            f"token_fp={token_fp} did={account.device_id}"
        )
        try:
            results = await client.sign_with_token(account.token)
            account.last_error = ""
            account.last_success_at = self._now_text()
            if account.auth_failed_notified:
                account.auth_failed_notified = False
            self._save_config()
            elapsed = int((datetime.now() - started).total_seconds() * 1000)
            logger.info(f"skland sign ok uid={account.name} roles={len(results)} elapsed_ms={elapsed}")
            for item in results:
                logger.info(
                    f"skland sign role uid={account.name} nickname={item.nickname} "
                    f"game={item.game_name} channel={item.channel_name} success={item.success} "
                    f"already_signed={item.already_signed} awards={item.awards} error={item.error}"
                )
            return self._format_account_result(account, results)
        except SklandAuthError as e:
            error = str(e)
            logger.warning(f"skland auth failed uid={account.name} owner={account.owner_user_id} error={error}")
            await self._notify_auth_failed_once(account, error)
            return self._format_account_result(account, error=f"认证失败: {error}")
        except Exception as e:
            account.last_error = str(e)
            self._save_config()
            logger.error(f"森空岛签到失败 uid={account.name}: {e}")
            return self._format_account_result(account, error=str(e))
        finally:
            await client.close()

    async def _run_sign(self, target_name: str = "") -> str:
        async with self._lock:
            accounts = self.cfg.accounts
            if target_name:
                accounts = [x for x in accounts if x.name == target_name]
            if not accounts:
                return "森空岛签到\n未配置账号"

            output: list[str] = []
            for account in accounts:
                output.extend(await self._run_one(account))
            return self._format_results("森空岛签到", output)

    async def _daily_tick(self):
        if not self.cfg.enabled or not self.cfg.accounts:
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if self.cfg.last_run_date == today:
            return
        if now.hour != self.cfg.hour or now.minute < self.cfg.minute:
            return

        logger.info(f"skland daily start date={today} accounts={len(self.cfg.accounts)}")
        await self._run_daily_sign()
        self.cfg.last_run_date = today
        self._save_config()
        logger.info(f"skland daily done date={today}")

    @command_registry.command("skland_sign", description="立即执行森空岛签到")
    @param(name="name", default="", help="账号名，可选")
    async def sign_cmd(self, event: BaseMessageEvent, name: str = ""):
        await event.reply(await self._run_sign(name.strip().strip("<>")))

    @command_registry.command("skland_status", description="[root] 查看森空岛签到配置")
    async def status_cmd(self, event: BaseMessageEvent):
        if not self._is_root(event):
            await event.reply("需要 root 权限")
            return
        lines = [
            "森空岛签到配置",
            f"状态: {'启用' if self.cfg.enabled else '禁用'}",
            f"定时: 每日 {self.cfg.hour:02d}:{self.cfg.minute:02d}",
            f"账号UID: {', '.join(a.name for a in self.cfg.accounts) or '未配置'}",
            f"上次定时: {self.cfg.last_run_date or '无'}",
        ]
        await event.reply("\n".join(lines))

    @command_registry.command("skland_config", description="配置森空岛签到 (add/remove 全员，其余需 root)")
    @param(name="action", default="", help="add/remove/hour/on/off/list")
    @param(name="name", default="", help="token 或参数")
    @param(name="value", default="", help="token 或参数")
    async def config_cmd(
        self,
        event: BaseMessageEvent,
        action: str = "",
        name: str = "",
        value: str = "",
    ):
        action = action.strip().lower()
        name = name.strip().strip("<>")
        value = value.strip().strip("<>")

        if action == "add":
            if not isinstance(event, PrivateMessageEvent):
                await event.reply("token 添加请私聊使用；群内登录请使用后续 QR/短信登录入口")
                return
            token = value or name
            if not token:
                await event.reply("用法: /skland_config add <鹰角token>")
                return
            notify_type, notify_id = self._event_notify_target(event)
            try:
                account, sign_text = await self._add_account_from_token(
                    token,
                    str(event.user_id),
                    notify_type,
                    notify_id,
                )
            except Exception as e:
                logger.error(f"添加森空岛账号失败: {e}")
                await event.reply(f"添加失败: {e}")
                return
            await event.reply(f"已添加森空岛账号 UID: {account.name}\n\n{sign_text}")
            return

        if action == "remove":
            if not self._can_manage_account(event, name):
                await event.reply("需要 root 权限或为该账号的添加者")
                return
            before = len(self.cfg.accounts)
            self.cfg.accounts = [a for a in self.cfg.accounts if a.name != name]
            self._save_config()
            await event.reply("已删除" if len(self.cfg.accounts) < before else "未找到账号")
            return

        if action in ("hour", "on", "off", "list"):
            if not self._is_root(event):
                await event.reply("需要 root 权限")
                return

        if action == "hour":
            try:
                hour = int(name)
            except ValueError:
                await event.reply("请输入 0-23 的整数")
                return
            if hour < 0 or hour > 23:
                await event.reply("请输入 0-23 的整数")
                return
            self.cfg.hour = hour
            self.cfg.minute = 1
            self._save_config()
            await event.reply(f"定时签到时间已设为每日 {hour:02d}:01")
            return

        if action in ("on", "off"):
            self.cfg.enabled = action == "on"
            self._save_config()
            await event.reply(f"森空岛签到已{'启用' if self.cfg.enabled else '禁用'}")
            return

        if action == "list":
            await self.status_cmd(event)
            return

        await event.reply(
            "用法:\n"
            "/skland_config add <鹰角token>  私聊添加，自动用角色 uid 命名\n"
            "/skland_config remove <uid>\n"
            "/skland_config hour <0-23>\n"
            "/skland_config on|off|list"
        )

    @command_registry.command("skland_sms", description="森空岛短信登录: <手机号>")
    @param(name="phone", default="", help="手机号")
    async def sms_cmd(self, event: BaseMessageEvent, phone: str = ""):
        phone = phone.strip().strip("<>")
        if not phone:
            await event.reply("用法: /skland_sms <手机号>")
            return
        notify_type, notify_id = self._event_notify_target(event)
        client = SklandClient()
        try:
            logger.info(
                f"skland sms send start owner={event.user_id} phone_hash={self._token_fingerprint(phone)} "
                f"notify_type={notify_type} notify_id={notify_id}"
            )
            await client.send_phone_code(phone)
        except Exception as e:
            logger.error(f"森空岛短信验证码发送失败 owner={event.user_id}: {e}")
            await event.reply(f"验证码发送失败: {e}")
            return
        finally:
            await client.close()

        self._sms_sessions[str(event.user_id)] = {
            "phone": phone,
            "notify_type": notify_type,
            "notify_id": notify_id,
            "created_at": time.time(),
        }
        await event.reply("验证码已发送，请在 5 分钟内发送 /skland_sms_code <验证码>")

    @command_registry.command("skland_sms_code", description="森空岛短信登录验证码: <验证码>")
    @param(name="code", default="", help="验证码")
    async def sms_code_cmd(self, event: BaseMessageEvent, code: str = ""):
        code = code.strip().strip("<>")
        if not code:
            await event.reply("用法: /skland_sms_code <验证码>")
            return

        session = self._sms_sessions.get(str(event.user_id))
        if not session:
            await event.reply("没有待完成的短信登录，请先使用 /skland_sms <手机号>")
            return
        if time.time() - float(session.get("created_at") or 0) > SMS_SESSION_TTL:
            self._sms_sessions.pop(str(event.user_id), None)
            await event.reply("短信登录已超时，请重新使用 /skland_sms <手机号>")
            return

        client = SklandClient()
        try:
            token = await client.get_token_by_phone_code(str(session["phone"]), code)
            account, sign_text = await self._add_account_from_token(
                token,
                str(event.user_id),
                str(session["notify_type"]),
                str(session["notify_id"]),
            )
        except Exception as e:
            logger.error(f"森空岛短信登录失败 owner={event.user_id}: {e}")
            await event.reply(f"短信登录失败: {e}")
            return
        finally:
            await client.close()

        self._sms_sessions.pop(str(event.user_id), None)
        await event.reply(f"短信登录成功，已添加森空岛账号 UID: {account.name}\n\n{sign_text}")

    @command_registry.command("skland_qr", description="森空岛二维码登录")
    async def qr_cmd(self, event: BaseMessageEvent):
        await event.reply(
            "二维码登录暂未启用：当前官网 Web SDK 的 QR/OAuth 流程只能稳定拿到一次性 code/森空岛 cred，"
            "还不能确认可取得长期鹰角 token；为避免保存短期凭据导致定时签到失效，暂不落库。"
        )


__all__ = ["SklandPlugin"]
