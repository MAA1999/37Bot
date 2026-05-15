"""arkrec 插件 —— 明日方舟少人wiki"""

import asyncio
import json
import re
from pathlib import Path

from ncatbot.plugin_system import NcatBotPlugin, command_registry, param
from ncatbot.core.event import GroupMessageEvent, PrivateMessageEvent
from ncatbot.utils import get_log

from .auth import ArkRecAuth
from .db import ArkRecDB
from .config import GroupSubscription
from .api import full_sync, incremental_sync, sync_exclusive

logger = get_log("ArkRec")

SYNC_INTERVAL = 120  # 增量同步间隔（秒）
EXCLUSIVE_INTERVAL = 86400  # 专属记录刷新间隔（秒）


class ArkRecPlugin(NcatBotPlugin):
    name = "ArkRecPlugin"
    version = "2.0.0"
    author = "Windsland52"
    dependencies = {}

    async def on_load(self):
        self.data_dir = self.workspace
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.db = ArkRecDB(self.data_dir / "arkrec.db")
        self._auth: ArkRecAuth | None = None
        self._synced = False
        self._last_exclusive_sync = 0.0

        # 加载配置
        self.cfg = self._load_config()
        self.subs: dict[str, GroupSubscription] = self._load_subscriptions()

        # 启动后台任务
        asyncio.create_task(self._sync_loop())

    # ====== 配置 ======

    def _load_config(self) -> dict:
        p = self.data_dir / "config.json"
        if p.exists():
            try:
                return json.loads(p.read_text("utf-8"))
            except Exception:
                pass
        return {"email": "", "password": ""}

    def _save_config(self):
        (self.data_dir / "config.json").write_text(
            json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_subscriptions(self) -> dict[str, GroupSubscription]:
        p = self.data_dir / "subscriptions.json"
        if p.exists():
            try:
                data = json.loads(p.read_text("utf-8"))
                return {gid: GroupSubscription(**g) for gid, g in data.items()}
            except Exception:
                pass
        return {}

    def _save_subscriptions(self):
        (self.data_dir / "subscriptions.json").write_text(
            json.dumps({gid: {
                "enabled": s.enabled, "categories": s.categories,
                "operators": s.operators, "operations": s.operations,
            } for gid, s in self.subs.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8")

    def _get_sub(self, group_id: str) -> GroupSubscription:
        if group_id not in self.subs:
            self.subs[group_id] = GroupSubscription()
        return self.subs[group_id]

    async def _get_auth(self):
        if self._auth is not None:
            return self._auth
        email = self.cfg.get("email", "")
        pw = self.cfg.get("password", "")
        if email and pw:
            self._auth = ArkRecAuth(self.data_dir, email, pw)
            return self._auth
        return None

    async def _is_admin(self, group_id: str, user_id: str) -> bool:
        try:
            info = await self.api.get_group_member_info(group_id, user_id)
            return info.role in ("owner", "admin")
        except Exception:
            return False

    # ====== 后台任务 ======

    async def _sync_loop(self):
        await asyncio.sleep(10)
        while True:
            try:
                auth = await self._get_auth()
                if auth:
                    client = await auth.get_client()

                    if not self._synced:
                        count = self.db.get_record_count()
                        if count == 0:
                            logger.info("数据库为空，开始全量同步...")
                            sem = asyncio.Semaphore(5)
                            await full_sync(self.db, client, sem)
                        self._synced = True

                    await incremental_sync(self.db, client)

                    # 检查专属记录是否需要刷新
                    if self.db.is_exclusive_stale():
                        try:
                            await sync_exclusive(self.db, client)
                        except Exception as e:
                            logger.warning(f"专属记录刷新失败: {e}")

                    await self._push_new_records(client)
                else:
                    logger.debug("未配置账号，跳过同步")
            except Exception as e:
                logger.error(f"同步异常: {e}")

            await asyncio.sleep(SYNC_INTERVAL)

    async def _push_new_records(self, client: "httpx.AsyncClient"):
        """检查新记录，匹配订阅并推送"""
        new_records = self.db.query_latest(limit=20)
        for group_id, sub in self.subs.items():
            if not sub.enabled:
                continue
            msgs = []
            for r in new_records:
                if self._matches_sub(r, sub):
                    msgs.append(self._format_record(r))
            for msg in msgs[:3]:  # 每次最多推 3 条
                try:
                    await self.api.post_group_msg(group_id, text=msg)
                except Exception as e:
                    logger.error(f"推送失败 group={group_id}: {e}")

    def _matches_sub(self, record: dict, sub: GroupSubscription) -> bool:
        cats = json.loads(record.get("category_json", "[]"))
        team = json.loads(record.get("team_json", "[]"))
        op_names = [t.get("name", "") for t in team]
        operation = record.get("operation", "")

        for c in sub.categories:
            if c in cats:
                return True
        for o in sub.operators:
            if o in op_names:
                return True
        for op in sub.operations:
            if op == operation:
                return True
        return False

    def _format_record(self, r: dict) -> str:
        team = json.loads(r.get("team_json", "[]"))
        cats = json.loads(r.get("category_json", "[]"))
        names = ",".join(t.get("name", "") for t in team)
        return (
            f"新纪录: {r['operation']} {r['cn_name']} ({r['operationType']})\n"
            f"阵容: {names}\n"
            f"分类: {','.join(cats)}\n"
            f"投稿: {r.get('raider', '')}\n"
            f"链接: {r.get('url', '')}"
        )

    # ====== 命令 ======

    @command_registry.command("arkrec_config", description="[root] 配置账号（私聊）")
    async def cmd_config(self, event: PrivateMessageEvent,
                         email: str = "", password: str = ""):
        if event.message_type != "private":
            await event.reply("请私聊使用此命令")
            return
        if not self.rbac_manager.user_has_role(str(event.user_id), "root"):
            await event.reply("需要 root 权限")
            return
        if not email or not password:
            await event.reply("用法: /arkrec_config <email> <password>")
            return
        self.cfg["email"] = email
        self.cfg["password"] = password
        self._save_config()
        self._auth = None
        await event.reply("账号已配置，下次同步生效")

    @command_registry.command("arkrec", description="查询少人记录 [干员/关卡/分类/数量]")
    @param(name="keyword", default="20", help="干员名、关卡号、分类名 或 查询数量")
    async def cmd_query(self, event: GroupMessageEvent, keyword: str = "20"):
        group_id = str(event.group_id)
        limit = 20
        operator = ""
        operation = ""
        category = ""

        # 判断输入类型
        kw = keyword.strip()
        if kw.isdigit():
            limit = max(1, min(int(kw), 50))
        elif re.match(r"^[A-Za-z]{1,4}[-_ ]?\d", kw):
            operation = kw.upper().replace(" ", "-")
        else:
            # 先查干员
            team_test = self.db.query_records(operator=kw, limit=1)
            if team_test:
                operator = kw
            else:
                # 再查分类
                cat_test = self.db.query_records(category=kw, limit=1)
                if cat_test:
                    category = kw
                else:
                    operation = kw.upper().replace(" ", "-")

        records = self.db.query_records(
            operator=operator, operation=operation, category=category, limit=limit)

        if not records:
            await event.reply(f'未找到 "{keyword}" 相关记录')
            return

        lines = [f'"{keyword}" 相关记录 ({len(records)}条):']
        for r in records[:10]:
            team = json.loads(r["team_json"])
            names = ",".join(t.get("name", "") for t in team[:5])
            cats = ",".join(json.loads(r["category_json"]))
            lines.append(
                f"\n{r['operation']} {r['cn_name']} [{cats}]\n"
                f"阵容: {names}\n"
                f"投稿: {r['raider']} | {r.get('url','')}"
            )
        if len(records) > 10:
            lines.append(f"\n... 共 {len(records)} 条，仅显示前 10 条")

        await event.reply("\n".join(lines))

    @command_registry.command("arkrec_op", description="查关卡信息")
    @param(name="operation", default="", help="关卡号 如 1-7")
    async def cmd_op(self, event: GroupMessageEvent, operation: str = ""):
        if not operation:
            await event.reply("用法: /arkrec_op <关卡号>")
            return
        ops = self.db.query_operations(keyword=operation.upper(), limit=5)
        if not ops:
            await event.reply(f'未找到关卡 "{operation}"')
            return
        for op in ops[:3]:
            records = self.db.query_records(operation=op["operation"], limit=5)
            cats = set()
            for r in records:
                cats.update(json.loads(r.get("category_json", "[]")))
            await event.reply(
                f"{op['operation']} {op['cn_name']}\n"
                f"活动: {op['story']} / {op['episode']}\n"
                f"记录数: {len(records)}+ | 流派: {', '.join(sorted(cats)[:8])}"
            )

    @command_registry.command("arkrec_sub", description="[管理员] 订阅推送: <分类/干员/关卡>")
    @param(name="value", default="", help="分类名、干员名 或 关卡号")
    async def cmd_sub(self, event: GroupMessageEvent, value: str = ""):
        if not await self._is_admin(event.group_id, event.user_id):
            await event.reply("需要群主或管理员权限")
            return
        if not value:
            await event.reply("用法: /arkrec_sub <分类名/干员名/关卡号>")
            return
        group_id = str(event.group_id)
        sub = self._get_sub(group_id)
        sub.enabled = True

        val = value.strip()
        if re.match(r"^[A-Za-z]{1,4}[-_ ]?\d", val):
            sub.operations.append(val.upper().replace(" ", "-"))
            await event.reply(f"已订阅关卡: {val}")
        elif self.db.query_records(category=val, limit=1):
            sub.categories.append(val)
            await event.reply(f"已订阅分类: {val}")
        else:
            sub.operators.append(val)
            await event.reply(f"已订阅干员: {val}")

        self._save_subscriptions()

    @command_registry.command("arkrec_unsub", description="[管理员] 取消订阅")
    @param(name="value", default="", help="要取消的分类/干员/关卡，留空取消全部")
    async def cmd_unsub(self, event: GroupMessageEvent, value: str = ""):
        if not await self._is_admin(event.group_id, event.user_id):
            await event.reply("需要群主或管理员权限")
            return
        group_id = str(event.group_id)
        if not value:
            self.subs.pop(group_id, None)
            self._save_subscriptions()
            await event.reply("已取消全部订阅")
            return
        sub = self._get_sub(group_id)
        val = value.strip()
        for lst in [sub.categories, sub.operators, sub.operations]:
            if val in lst:
                lst.remove(val)
        self._save_subscriptions()
        await event.reply(f"已取消: {val}")

    @command_registry.command("arkrec_status", description="查看订阅状态和数据库统计")
    async def cmd_status(self, event: GroupMessageEvent):
        group_id = str(event.group_id)
        sub = self.subs.get(group_id, GroupSubscription())
        count = self.db.get_record_count()
        lines = [
            f"ArkRec 数据库: {count} 条记录",
            f"本群订阅: {'启用' if sub.enabled else '未启用'}",
        ]
        if sub.enabled:
            if sub.categories:
                lines.append(f"  分类: {', '.join(sub.categories)}")
            if sub.operators:
                lines.append(f"  干员: {', '.join(sub.operators)}")
            if sub.operations:
                lines.append(f"  关卡: {', '.join(sub.operations)}")
            if not any([sub.categories, sub.operators, sub.operations]):
                lines.append("  (未设置筛选条件)")
        await event.reply("\n".join(lines))

    @command_registry.command("arkrec_exclusive", description="查某关专属记录")
    @param(name="operation", default="", help="关卡号 如 1-7")
    @param(name="category", default="", help="流派筛选（可选）")
    async def cmd_exclusive(self, event: GroupMessageEvent,
                            operation: str = "", category: str = ""):
        if not operation:
            await event.reply("用法: /arkrec_exclusive <关卡号> [流派]")
            return
        records = self.db.query_exclusive(
            operation=operation.upper().replace(" ", "-"), category=category)
        if not records:
            await event.reply(f'未找到 "{operation}" 专属记录，请检查关卡号或稍后刷新')
            return
        lines = [f"{operation} 专属记录:"]
        for r in records[:15]:
            ops = json.loads(r["operators_json"])
            lines.append(
                f"[{r['category']}] {r['mode']}: {', '.join(ops)}")
        await event.reply("\n".join(lines))


__all__ = ["ArkRecPlugin"]
