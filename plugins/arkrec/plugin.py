"""arkrec 插件 —— 明日方舟少人wiki"""

import asyncio
import json
import re
import threading
import time
from pathlib import Path

import httpx
from ncatbot.plugin_system import NcatBotPlugin, command_registry, on_message, param
from ncatbot.core.event import GroupMessageEvent, PrivateMessageEvent
from ncatbot.core.event.message_segment import Reply
from ncatbot.utils import get_log

from .auth import ArkRecAuth
from .db import ArkRecDB
from .config import GroupSubscription
from .api import full_sync, incremental_sync

logger = get_log("ArkRec")

SYNC_INTERVAL = 120  # 增量同步间隔（秒）
LINK_CACHE_TTL = 3600
LINK_CACHE_LIMIT = 200

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)


def _team_member_name(member) -> str:
    if isinstance(member, dict):
        return member.get("name", "")
    if isinstance(member, str):
        return member
    return ""


class ArkRecPlugin(NcatBotPlugin):
    name = "ArkRecPlugin"
    version = "2.0.0"
    author = "Windsland52"
    dependencies = {}

    async def on_load(self):
        logger.info("ArkRec on_load start")
        self.data_dir = self.workspace
        self.data_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"ArkRec workspace: {self.data_dir}")

        self.db = ArkRecDB(self.data_dir / "arkrec.db")
        self._auth: ArkRecAuth | None = None
        self._sync_lock = threading.Lock()
        self._synced = False

        # 加载配置
        self.cfg = self._load_config()
        self.subs: dict[str, GroupSubscription] = self._load_subscriptions()
        self._pushed_state: dict[str, str]
        self._pending_ids: dict[str, list[str]]
        self._pushed_state, self._pending_ids = self._load_push_state()
        self._link_cache: dict[str, dict] = {}

        self.add_scheduled_task(
            self._sync_once,
            "arkrec_sync_initial",
            "10s",
            max_runs=1,
        )
        self.add_scheduled_task(
            self._sync_once,
            "arkrec_sync",
            f"{SYNC_INTERVAL}s",
        )
        logger.info("ArkRec scheduled sync tasks registered")

    async def on_close(self, *args, **kwargs):
        logger.info("ArkRec on_close start")
        if self._auth:
            await self._auth.close()
            self._auth = None

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

    def _load_push_state(self) -> tuple[dict[str, str], dict[str, list[str]]]:
        p = self.data_dir / "push_state.json"
        if p.exists():
            try:
                data = json.loads(p.read_text("utf-8"))
                return data.get("pushed", {}), data.get("pending", {})
            except Exception:
                pass
        return {}, {}

    def _save_push_state(self):
        (self.data_dir / "push_state.json").write_text(json.dumps({
            "pushed": self._pushed_state,
            "pending": self._pending_ids,
        }, ensure_ascii=False), encoding="utf-8")

    def _create_tourist_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"User-Agent": CHROME_UA},
            timeout=30,
        )

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

    async def _check_admin_or_root(self, group_id: str, user_id: str) -> bool:
        """root 直接放行；否则需是群主/管理员"""
        if self.rbac_manager.user_has_role(str(user_id), "root"):
            return True
        try:
            info = await self.api.get_group_member_info(group_id, user_id)
            return info.role in ("owner", "admin")
        except Exception:
            return False

    # ====== 后台任务 ======

    async def _sync_once(self):
        if not self._sync_lock.acquire(blocking=False):
            logger.info("ArkRec sync skipped: previous sync still running")
            return
        try:
            logger.info("ArkRec sync tick")
            async with self._create_tourist_client() as client:
                if not self._synced:
                    count = self.db.get_record_count()
                    logger.info(f"ArkRec record count before initial sync: {count}")
                    if count == 0:
                        logger.info("数据库为空，开始全量同步...")
                        sem = asyncio.Semaphore(5)
                        await full_sync(self.db, client, sem)
                    self._synced = True

                new_ids = await incremental_sync(self.db, client)
                logger.info(f"ArkRec incremental done: {len(new_ids)} new ids")
                await self._push_new_records(new_ids)
                logger.info("ArkRec push state saved")
        except Exception as e:
            logger.error(f"同步异常: {e}")
        finally:
            self._sync_lock.release()

    async def _push_new_records(self, new_ids: list[str]):
        """推送匹配订阅的记录，每群每轮最多 3 条。
        未发送的暂存到 per-group pending 队列，下轮优先补推 pending 再推新记录。
        """
        pending_total = sum(len(ids) for ids in self._pending_ids.values())
        logger.info(
            f"ArkRec push check: new_ids={len(new_ids)}, "
            f"groups={len(self.subs)}, pending={pending_total}"
        )
        new_records = self.db.get_records_by_ids(new_ids) if new_ids else []

        for group_id, sub in self.subs.items():
            if not sub.enabled:
                continue

            # 收集 pending 候选（上次未发完的），按 _id 升序
            pending = self._pending_ids.get(group_id, [])
            pending_records = self.db.get_records_by_ids(pending) if pending else []
            pending_matched = []
            for r in pending_records:
                if self._matches_sub(r, sub):
                    pending_matched.append((r["_id"], self._format_record(r)))
            pending_matched.sort(key=lambda x: x[0])

            # 本轮新增匹配，同样按 _id 升序
            new_matched = []
            for r in new_records:
                if self._matches_sub(r, sub):
                    new_matched.append((r["_id"], self._format_record(r)))
            new_matched.sort(key=lambda x: x[0])

            # pending 优先 + 新记录追加
            all_candidates = pending_matched + new_matched

            # 发送前 3 条
            sent_ids = []
            failed_ids = []
            for rid, msg in all_candidates[:3]:
                try:
                    await self.api.post_group_msg(group_id, text=msg)
                    sent_ids.append(rid)
                except Exception as e:
                    logger.error(f"推送失败 group={group_id}: {e}")
                    failed_ids.append(rid)

            # 发送失败的 + 超出 3 条的都回到 pending
            unsent = failed_ids + [rid for rid, _ in all_candidates[3:]]
            if unsent:
                self._pending_ids[group_id] = unsent
            else:
                self._pending_ids.pop(group_id, None)

            # 游标只推进到本轮已发送的最大 _id
            if sent_ids:
                max_sent = max(sent_ids)
                last_id = self._pushed_state.get(group_id, "")
                if max_sent > last_id:
                    self._pushed_state[group_id] = max_sent

        self._save_push_state()

    def _matches_sub(self, record: dict, sub: GroupSubscription) -> bool:
        cats = json.loads(record.get("category_json", "[]"))
        team = json.loads(record.get("team_json", "[]"))
        op_names = [_team_member_name(t) for t in team]
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
        names = ",".join(_team_member_name(t) for t in team)
        return (
            f"新纪录: {r['operation']} {r['cn_name']} ({r['operationType']})\n"
            f"阵容: {names}\n"
            f"分类: {','.join(cats)}\n"
            f"投稿: {r.get('raider', '')}\n"
            f"链接: {r.get('url', '')}"
        )

    @staticmethod
    def _extract_message_id(resp) -> str:
        if resp is None:
            return ""
        if isinstance(resp, (str, int)):
            return str(resp)
        if isinstance(resp, dict):
            for key in ("message_id", "id"):
                if resp.get(key) is not None:
                    return str(resp[key])
            data = resp.get("data")
            if isinstance(data, dict):
                for key in ("message_id", "id"):
                    if data.get(key) is not None:
                        return str(data[key])
        for key in ("message_id", "id"):
            value = getattr(resp, key, None)
            if value is not None:
                return str(value)
        return ""

    def _prune_link_cache(self):
        now = time.time()
        expired = [
            mid for mid, item in self._link_cache.items()
            if now - item.get("created_at", now) > LINK_CACHE_TTL
        ]
        for mid in expired:
            self._link_cache.pop(mid, None)
        if len(self._link_cache) > LINK_CACHE_LIMIT:
            ordered = sorted(
                self._link_cache.items(),
                key=lambda item: item[1].get("created_at", 0),
            )
            for mid, _ in ordered[: len(self._link_cache) - LINK_CACHE_LIMIT]:
                self._link_cache.pop(mid, None)

    async def _reply_records(self, event: GroupMessageEvent, text: str, records: list[dict]):
        resp = await event.reply(text)
        message_id = self._extract_message_id(resp)
        if not message_id:
            logger.warning(f"ArkRec query reply message_id missing: {resp!r}")
            return
        links = []
        has_url = False
        for i, r in enumerate(records, 1):
            url = r.get("url", "")
            if url:
                has_url = True
            links.append({
                "index": i,
                "_id": r.get("_id", ""),
                "operation": r.get("operation", ""),
                "cn_name": r.get("cn_name", ""),
                "raider": r.get("raider", ""),
                "url": url,
            })
        if has_url:
            self._link_cache[message_id] = {
                "group_id": str(event.group_id),
                "created_at": time.time(),
                "links": links,
            }
            self._prune_link_cache()

    def _get_replied_message_id(self, event: GroupMessageEvent) -> str:
        for seg in event.message.filter(Reply):
            if getattr(seg, "id", None):
                return str(seg.id)
        return ""

    @on_message
    async def _on_link_reply(self, event):
        if not isinstance(event, GroupMessageEvent):
            return
        replied_id = self._get_replied_message_id(event)
        if not replied_id:
            return
        cache = self._link_cache.get(replied_id)
        if not cache or cache.get("group_id") != str(event.group_id):
            return
        self._prune_link_cache()
        if replied_id not in self._link_cache:
            return

        text = event.message.concatenate_text().strip()
        match = re.search(r"\d+", text)
        if match:
            index = int(match.group(0))
        elif len(cache["links"]) == 1:
            index = 1
        else:
            await event.reply("请回复记录序号，如 1")
            return

        links = cache["links"]
        if index < 1 or index > len(links):
            await event.reply(f"序号范围: 1-{len(links)}")
            return
        item = links[index - 1]
        if not item["url"]:
            await event.reply(f"[{index}] 这条记录没有链接")
            return
        await event.reply(
            f"[{index}] {item['operation']} {item['cn_name']}\n"
            f"投稿: {item['raider']}\n"
            f"{item['url']}"
        )

    # ====== 关卡名称解析 ======

    @staticmethod
    def _normalize_op(name: str) -> str:
        """去符号去空格统一大写，H17-4 → H174, GT-HX-1 → GTHX1"""
        return re.sub(r"[^A-Za-z0-9]", "", name).upper()

    def _resolve_operation(self, kw: str) -> str:
        """将简写解析为标准关卡号，如 h174 → H17-4"""
        upper = kw.upper().replace(" ", "-")
        norm = self._normalize_op(kw)
        matches = self.db.resolve_operation(norm)
        if matches:
            return matches[0]
        return upper

    # ====== 新旧分类 ======

    @staticmethod
    def _mark_current(records: list[dict]) -> list[dict]:
        """同 (operation, category, mode) 下最少人(解手流最少步)标记为当前，其余为旧。
        每个记录附加 _current_cats 集合，表示该记录在哪些分类下是当前纪录。"""
        def parse_step(remark: str) -> int:
            m = re.search(r"(\d+)步", remark)
            return int(m.group(1)) if m else 99

        groups: dict[tuple, list[dict]] = {}
        for r in records:
            cats = json.loads(r.get("category_json", "[]"))
            for cat in cats:
                key = (r["operation"], cat, r.get("operationType", ""))
                groups.setdefault(key, []).append(r)
        best_ids: dict[tuple, set] = {}  # key → set of best _ids (ties included)
        for key, recs in groups.items():
            if "解手流" in key[1]:
                min_step = min(parse_step(r.get("remark1", "")) for r in recs)
                best_ids[key] = {r["_id"] for r in recs if parse_step(r.get("remark1", "")) == min_step}
            else:
                min_size = min(len(json.loads(r.get("team_json", "[]"))) for r in recs)
                best_ids[key] = {r["_id"] for r in recs if len(json.loads(r.get("team_json", "[]"))) == min_size}
        for r in records:
            cats = json.loads(r.get("category_json", "[]"))
            r["_current_cats"] = {cat for cat in cats
                                   if r["_id"] in best_ids.get((r["operation"], cat, r.get("operationType", "")), set())}
        return records

    # ====== 命令 ======

    @command_registry.command("arkrec_config", description="[root] 配置账号（私聊）")
    async def cmd_config(self, event: PrivateMessageEvent,
                         email: str = "", password: str = ""):
        """注意：密码以明文保存在 data/ArkRecPlugin/config.json，仅用于 wiki 登录。
        公开数据同步不需要账号，此功能为可选。"""
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

    @command_registry.command("arkrec", description="查询记录: [关卡] [分类] [干员]")
    @param(name="p1", default="", help="关卡号 / 分类 / 干员名")
    @param(name="p2", default="", help="可选")
    @param(name="p3", default="", help="可选")
    @param(name="p4", default="", help="可选")
    async def cmd_query(self, event: GroupMessageEvent, p1: str = "",
                        p2: str = "", p3: str = "", p4: str = ""):
        logger.info(f"arkrec query: p1={p1!r} p2={p2!r} p3={p3!r} p4={p4!r}")
        operator = ""
        operation = ""
        category = ""
        mode = ""  # ""=全部, "normal", "challenge"
        grp = ""   # ""=全部, "沙盘推演", etc.

        parts = [p for p in [p1, p2, p3, p4] if p]
        if not parts:
            category = "常规队"
            records = self.db.query_records(category=category, limit=200)
            records = self._mark_current(records)
            if records:
                lines = [f"最近常规队记录:"]
                display_records = records[:20]
                for i, r in enumerate(display_records, 1):
                    cats = ",".join(json.loads(r["category_json"]))
                    mode_label = "突袭" if r["operationType"] == "challenge" else ""
                    is_current = any(category in c for c in r.get("_current_cats", set())) if category else bool(r.get("_current_cats"))
                    tag = "" if is_current else " [旧]"
                    team = json.loads(r["team_json"])
                    names = ",".join(_team_member_name(t) for t in team[:5])
                    lines.append(
                        f"\n[{i}] {r['operation']} {r['cn_name']} {mode_label} [{cats}]{tag}\n"
                        f"阵容: {names}\n"
                        f"投稿: {r['raider']}"
                    )
                lines.append("\n回复本条消息序号可查看链接")
                await self._reply_records(event, "\n".join(lines), display_records)
            else:
                await event.reply("暂无记录")
            return

        for kw in parts:
            if kw in ("沙盘", "沙盘推演"):
                grp = "沙盘推演"
            elif kw in ("突袭", "challenge", "磨难", "险地", "磨难险地"):
                mode = "challenge"
            elif kw in ("普通", "normal", "标准"):
                mode = "normal"
            elif re.match(r"^[A-Za-z]?[0-9]+[-_ ]?[0-9]*$", kw, re.IGNORECASE):
                if not operation:
                    operation = self._resolve_operation(kw)
            elif not category and self.db.query_records(category=kw, limit=1):
                category = kw
            elif not operator and self.db.query_records(operator=kw, limit=1):
                operator = kw
            elif not operation:
                operation = self._resolve_operation(kw)

        # 无分类筛选时默认常规队
        if not category and not operator:
            category = "常规队"

        records = self.db.query_records(
            operator=operator, operation=operation, category=category, mode=mode, grp=grp, limit=200)

        # 基于全关卡数据判当前纪录，避免在筛选子集里误判
        if operation:
            full = self.db.query_records(operation=operation, limit=500)
            full = self._mark_current(full)
            cur_map = {r["_id"]: r.get("_current_cats", set()) for r in full}
            for r in records:
                r["_current_cats"] = cur_map.get(r["_id"], set())
        else:
            records = self._mark_current(records)

        filters = " ".join(f for f in [operation, category, operator] if f)
        if not records:
            await event.reply(f"未找到 {' '.join(parts)} 相关记录")
            return

        lines = [f'"{filters}" ({len(records)}条):']
        display_records = records[:20]
        for i, r in enumerate(display_records, 1):
            team = json.loads(r["team_json"])
            names = ",".join(_team_member_name(t) for t in team[:5])
            cats = ",".join(json.loads(r["category_json"]))
            mode = "突袭" if r["operationType"] == "challenge" else ""
            is_current = any(category in c for c in r.get("_current_cats", set())) if category else bool(r.get("_current_cats"))
            tag = "" if is_current else " [旧]"
            lines.append(
                f"\n[{i}] {r['operation']} {r['cn_name']} {mode} [{cats}]{tag}\n"
                f"阵容: {names}\n"
                f"投稿: {r['raider']}"
            )
        if len(records) > 20:
            lines.append(f"\n... 共 {len(records)} 条，仅显示前 20 条")
        lines.append("\n回复本条消息序号可查看链接")

        await self._reply_records(event, "\n".join(lines), display_records)

    @command_registry.command("arkrec_top", description="最近 N 条记录")
    @param(name="count", default="20", help="数量")
    @param(name="category", default="", help="筛选分类")
    async def cmd_top(self, event: GroupMessageEvent, count: str = "20",
                      category: str = ""):
        try:
            n = max(1, min(int(count), 50))
        except ValueError:
            n = 20
        if category:
            records = self.db.query_records(category=category, limit=n)
        else:
            records = self.db.query_latest(limit=n)
        if not records:
            await event.reply("暂无记录")
            return
        lines = [f"最近 {n} 条记录:"]
        display_records = records[:20]
        for i, r in enumerate(display_records, 1):
            team = json.loads(r["team_json"])
            names = ",".join(_team_member_name(t) for t in team[:5])
            cats = ",".join(json.loads(r["category_json"]))
            lines.append(
                f"\n[{i}] {r['operation']} {r['cn_name']} [{cats}]\n"
                f"阵容: {names}\n"
                f"投稿: {r['raider']}"
            )
        lines.append("\n回复本条消息序号可查看链接")
        await self._reply_records(event, "\n".join(lines), display_records)

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
        if not await self._check_admin_or_root(event.group_id, event.user_id):
            await event.reply("需要群主/管理员或 root 权限")
            return
        if not value:
            await event.reply("用法: /arkrec_sub <分类名/干员名/关卡号>")
            return
        group_id = str(event.group_id)
        sub = self._get_sub(group_id)
        sub.enabled = True

        val = value.strip()
        if re.match(r"^[A-Za-z]{1,4}[-_ ]?\d", val):
            op_val = val.upper().replace(" ", "-")
            if op_val not in sub.operations:
                sub.operations.append(op_val)
            await event.reply(f"已订阅关卡: {val}")
        elif self.db.query_records(category=val, limit=1):
            if val not in sub.categories:
                sub.categories.append(val)
            await event.reply(f"已订阅分类: {val}")
        else:
            if val not in sub.operators:
                sub.operators.append(val)
            await event.reply(f"已订阅干员: {val}")

        self._save_subscriptions()

    @command_registry.command("arkrec_unsub", description="[管理员] 取消订阅")
    @param(name="value", default="", help="要取消的分类/干员/关卡，留空取消全部")
    async def cmd_unsub(self, event: GroupMessageEvent, value: str = ""):
        if not await self._check_admin_or_root(event.group_id, event.user_id):
            await event.reply("需要群主/管理员或 root 权限")
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

__all__ = ["ArkRecPlugin"]
