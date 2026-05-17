"""arkrec 插件 —— 明日方舟少人wiki"""

import asyncio
import html
import json
import re
import threading
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright
from ncatbot.plugin_system import NcatBotPlugin, command_registry, on_message, param
from ncatbot.core.event import GroupMessageEvent, PrivateMessageEvent
from ncatbot.core.event.message_segment import Reply
from ncatbot.utils import get_log

from .auth import ArkRecAuth
from .db import ArkRecDB
from .config import GroupSubscription
from .api import (
    fetch_bundle_ext,
    fetch_exclusive_operators,
    fetch_open_episodes,
    fetch_operation_info,
    full_sync,
    incremental_sync,
)

logger = get_log("ArkRec")

SYNC_INTERVAL = 120  # 增量同步间隔（秒）
LINK_CACHE_TTL = 3600
LINK_CACHE_LIMIT = 200
RECORD_IMAGE_MIN_ROWS = 2
EXCLUSIVE_CACHE_TTL = 6 * 3600
EXCLUSIVE_IMAGE_MAX_ROWS = 24
EXCLUSIVE_ALWAYS_OPEN_STORIES = {"主线关卡", "剿灭作战", "物资筹备", "芯片搜索"}
BRIEF_CACHE_TTL = 6 * 3600
BRIEF_IMAGE_MAX_ROWS = 60

CATEGORY_ALIASES = {
    "wzw": "毋作吾",
    "201": "精二1级",
    "180": "精一满级",
    "101": "精一1级",
    "无精满": "无精英满级",
    "2015": "精二1级五星队",
    "1604": "精一满级四星队",
    "1014": "精一1级四星队",
    "无精满四星": "无精英满级四星队",
    "自忍": "自闭忍宗",
    "孤忍": "孤岛忍宗",
    "深海": "深海猎人队",
}

CATEGORY_SUFFIXES = ("队", "级", "流", "组")

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
        self.image_dir = self.data_dir / "images"
        self.image_dir.mkdir(parents=True, exist_ok=True)
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
        self._exclusive_cache: dict = {"created_at": 0.0, "data": []}
        self._open_episodes_cache: dict = {"created_at": 0.0, "data": []}
        self._operation_info_cache: dict = {"created_at": 0.0, "data": []}
        self._bundle_ext_cache: dict = {"created_at": 0.0, "data": {}}
        self._exclusive_lock = asyncio.Lock()
        self._open_episodes_lock = asyncio.Lock()
        self._operation_info_lock = asyncio.Lock()
        self._bundle_ext_lock = asyncio.Lock()

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

    async def _get_exclusive_operators(self, force: bool = False) -> list[dict]:
        now = time.time()
        cached = self._exclusive_cache.get("data") or []
        created_at = float(self._exclusive_cache.get("created_at") or 0)
        if cached and not force and now - created_at < EXCLUSIVE_CACHE_TTL:
            return cached

        async with self._exclusive_lock:
            now = time.time()
            cached = self._exclusive_cache.get("data") or []
            created_at = float(self._exclusive_cache.get("created_at") or 0)
            if cached and not force and now - created_at < EXCLUSIVE_CACHE_TTL:
                return cached
            async with self._create_tourist_client() as client:
                data = await fetch_exclusive_operators(client)
            self._exclusive_cache = {"created_at": time.time(), "data": data}
            logger.info(f"ArkRec exclusive cache refreshed: {len(data)} operators")
            return data

    async def _get_open_episodes(self, force: bool = False) -> set[str]:
        now = time.time()
        cached = self._open_episodes_cache.get("data") or []
        created_at = float(self._open_episodes_cache.get("created_at") or 0)
        if cached and not force and now - created_at < EXCLUSIVE_CACHE_TTL:
            return set(str(item) for item in cached)

        async with self._open_episodes_lock:
            now = time.time()
            cached = self._open_episodes_cache.get("data") or []
            created_at = float(self._open_episodes_cache.get("created_at") or 0)
            if cached and not force and now - created_at < EXCLUSIVE_CACHE_TTL:
                return set(str(item) for item in cached)
            async with self._create_tourist_client() as client:
                data = await fetch_open_episodes(client)
            self._open_episodes_cache = {"created_at": time.time(), "data": data}
            logger.info(f"ArkRec open episodes cache refreshed: {len(data)} episodes")
            return set(str(item) for item in data)

    async def _get_operation_info(self, force: bool = False) -> list[dict]:
        now = time.time()
        cached = self._operation_info_cache.get("data") or []
        created_at = float(self._operation_info_cache.get("created_at") or 0)
        if cached and not force and now - created_at < BRIEF_CACHE_TTL:
            return cached

        async with self._operation_info_lock:
            now = time.time()
            cached = self._operation_info_cache.get("data") or []
            created_at = float(self._operation_info_cache.get("created_at") or 0)
            if cached and not force and now - created_at < BRIEF_CACHE_TTL:
                return cached
            async with self._create_tourist_client() as client:
                data = await fetch_operation_info(client)
            self._operation_info_cache = {"created_at": time.time(), "data": data}
            logger.info(f"ArkRec operation info cache refreshed: {len(data)} rows")
            return data

    async def _get_bundle_ext(self, force: bool = False) -> dict:
        now = time.time()
        cached = self._bundle_ext_cache.get("data") or {}
        created_at = float(self._bundle_ext_cache.get("created_at") or 0)
        if cached and not force and now - created_at < BRIEF_CACHE_TTL:
            return cached

        async with self._bundle_ext_lock:
            now = time.time()
            cached = self._bundle_ext_cache.get("data") or {}
            created_at = float(self._bundle_ext_cache.get("created_at") or 0)
            if cached and not force and now - created_at < BRIEF_CACHE_TTL:
                return cached
            async with self._create_tourist_client() as client:
                data = await fetch_bundle_ext(client)
            self._bundle_ext_cache = {"created_at": time.time(), "data": data}
            logger.info("ArkRec bundle-ext cache refreshed")
            return data

    async def _get_operation_rows_from_menu(self, force: bool = False) -> list[dict]:
        rows = self.db.query_operations(limit=10000)
        if rows and not force:
            return rows
        async with self._create_tourist_client() as client:
            ops = await fetch_menu(client)
        if ops:
            self.db.upsert_operations(ops)
        return self.db.query_operations(limit=10000)

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
        difficulty = self._record_difficulty_label(r) or "普通"
        return (
            f"新纪录: {r['operation']} {r['cn_name']} ({difficulty})\n"
            f"阵容: {names}\n"
            f"分类: {','.join(cats)}\n"
            f"投稿: {r.get('raider', '')}\n"
            f"链接: {r.get('url', '')}"
        )

    @staticmethod
    def _record_image_html(
        title: str,
        records: list[dict],
        old_count: int = 0,
        truncated_count: int = 0,
        show_old_tag: bool = False,
    ) -> str:
        def esc(value) -> str:
            return html.escape(str(value or ""), quote=True)

        rows = []
        for i, r in enumerate(records, 1):
            team = json.loads(r.get("team_json", "[]"))
            categories = json.loads(r.get("category_json", "[]"))
            names = "、".join(
                _team_member_name(t) for t in team[:5] if _team_member_name(t)
            )
            difficulty = ArkRecPlugin._record_difficulty_label(r)
            badges = []
            if difficulty:
                badges.append(f'<span class="badge badge-mode">{esc(difficulty)}</span>')
            if show_old_tag:
                badges.append('<span class="badge badge-old">旧</span>')
            cat_html = "".join(
                f'<span class="chip">{esc(cat)}</span>' for cat in categories[:8]
            )
            remark = r.get("remark1", "")
            remark_html = (
                f'<div class="meta remark"><span>备注</span>{esc(remark)}</div>'
                if remark else ""
            )
            rows.append(f"""
<section class="record">
  <div class="idx">{i}</div>
  <div class="main">
    <div class="head">
      <span class="op">{esc(r.get("operation", ""))}</span>
      <span class="name">{esc(r.get("cn_name", ""))}</span>
      {''.join(badges)}
    </div>
    <div class="cats">{cat_html}</div>
    <div class="meta"><span>阵容</span>{esc(names or "-")}</div>
    <div class="meta"><span>投稿</span>{esc(r.get("raider", "") or "-")}</div>
    {remark_html}
  </div>
</section>""")

        notes = []
        if truncated_count > 0:
            notes.append(f"当前纪录共 {truncated_count} 条，仅显示前 {len(records)} 条")
        if old_count > 0:
            notes.append(f"旧纪录 {old_count} 条，回复本消息“旧”可查看")
        notes.append("回复本消息序号可查看链接")
        note_html = "".join(f"<div>{esc(note)}</div>" for note in notes)

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  padding: 24px;
  background:
    linear-gradient(135deg, rgba(31, 111, 235, .12), rgba(245, 158, 11, .08) 42%, rgba(15, 23, 42, .04)),
    radial-gradient(circle at 14% 10%, rgba(31, 111, 235, .16), transparent 26%),
    radial-gradient(circle at 88% 8%, rgba(20, 184, 166, .12), transparent 24%),
    linear-gradient(rgba(255,255,255,.55) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,255,255,.55) 1px, transparent 1px),
    #eef2f7;
  background-size: auto, auto, auto, 18px 18px, 18px 18px, auto;
  color: #172033;
  font-family: "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", sans-serif;
}}
.card {{
  width: 760px;
  margin: 0 auto;
  background: rgba(255, 255, 255, .96);
  border: 1px solid rgba(212, 220, 232, .95);
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 18px 46px rgba(15, 23, 42, .16), 0 2px 8px rgba(15, 23, 42, .06);
}}
.title {{
  padding: 18px 22px 14px;
  border-bottom: 1px solid #e5eaf1;
  background:
    linear-gradient(90deg, rgba(31, 111, 235, .10), rgba(20, 184, 166, .07)),
    #f8fafc;
  font-size: 22px;
  font-weight: 700;
  letter-spacing: 0;
}}
.records {{ padding: 12px 14px 6px; }}
.record {{
  display: grid;
  grid-template-columns: 34px 1fr;
  gap: 12px;
  padding: 12px 8px;
  border-bottom: 1px solid #edf0f5;
}}
.record:last-child {{ border-bottom: 0; }}
.idx {{
  width: 28px;
  height: 28px;
  border-radius: 6px;
  background: #1f6feb;
  color: #fff;
  font-size: 16px;
  font-weight: 700;
  line-height: 28px;
  text-align: center;
}}
.head {{
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-height: 30px;
}}
.op {{ font-size: 18px; font-weight: 700; color: #0f172a; }}
.name {{ font-size: 18px; font-weight: 650; color: #1f2937; }}
.badge, .chip {{
  display: inline-block;
  border-radius: 5px;
  padding: 2px 7px;
  font-size: 13px;
  line-height: 18px;
  white-space: nowrap;
}}
.badge-mode {{ background: #fff1d6; color: #8a4b00; border: 1px solid #ffd58a; }}
.badge-old {{ background: #f1f5f9; color: #64748b; border: 1px solid #d8e0ea; }}
.cats {{ margin-top: 6px; display: flex; gap: 5px; flex-wrap: wrap; }}
.chip {{ background: #eef6ff; color: #175da8; border: 1px solid #cfe4fb; }}
.meta {{
  margin-top: 7px;
  font-size: 15px;
  line-height: 1.45;
  color: #334155;
  word-break: break-word;
}}
.meta span {{
  display: inline-block;
  min-width: 40px;
  margin-right: 8px;
  color: #64748b;
}}
.footer {{
  border-top: 1px solid #e5eaf1;
  background: #fbfcfe;
  padding: 12px 22px 16px;
  font-size: 14px;
  color: #64748b;
  line-height: 1.7;
}}
</style>
</head>
<body>
<div class="card">
  <div class="title">{esc(title)}</div>
  <div class="records">{''.join(rows)}</div>
  <div class="footer">{note_html}</div>
</div>
</body>
</html>"""

    async def _render_records_image(
        self,
        title: str,
        records: list[dict],
        old_count: int = 0,
        truncated_count: int = 0,
        show_old_tag: bool = False,
    ) -> Path | None:
        if len(records) < RECORD_IMAGE_MIN_ROWS:
            return None
        html_doc = self._record_image_html(
            title, records, old_count, truncated_count, show_old_tag
        )
        png_path = self.image_dir / f"arkrec_{int(time.time() * 1000)}.png"
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page(viewport={"width": 808, "height": 800})
                await page.set_content(html_doc, wait_until="networkidle")
                card = await page.query_selector(".card")
                if card:
                    box = await card.bounding_box()
                    if box:
                        await page.set_viewport_size({
                            "width": 808,
                            "height": min(max(int(box["height"]) + 48, 360), 3000),
                        })
                await page.screenshot(path=str(png_path), full_page=True)
                await browser.close()
            return png_path
        except Exception as e:
            logger.error(f"ArkRec render records image failed: {e}")
            return None

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

    async def _reply_records(
        self,
        event: GroupMessageEvent,
        text: str,
        records: list[dict],
        old_records: list[dict] | None = None,
        show_old_tag: bool = False,
    ):
        title = text.splitlines()[0] if text else "ArkRec 记录"
        truncated_count = 0
        match = re.search(r"当前纪录共\s*(\d+)\s*条", text)
        if match:
            truncated_count = int(match.group(1))
        image_path = await self._render_records_image(
            title,
            records,
            old_count=len(old_records or []),
            truncated_count=truncated_count,
            show_old_tag=show_old_tag,
        )
        if image_path:
            try:
                resp = await event.reply(f"[CQ:image,file={image_path.resolve().as_posix()}]")
            except Exception as e:
                logger.error(f"ArkRec send records image failed: {e}")
                resp = await event.reply(text)
            finally:
                try:
                    image_path.unlink(missing_ok=True)
                except Exception:
                    pass
        else:
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
        if has_url or old_records:
            self._link_cache[message_id] = {
                "group_id": str(event.group_id),
                "created_at": time.time(),
                "links": links,
                "old_records": old_records or [],
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
        if text in ("旧", "旧纪录", "旧记录", "old"):
            old_records = cache.get("old_records", [])
            if not old_records:
                await event.reply("没有旧纪录")
                return
            display_records = old_records[:20]
            lines = [f"旧纪录 ({len(old_records)}条):"]
            lines.extend(
                self._format_record_line(r, i, "", show_old_tag=True)
                for i, r in enumerate(display_records, 1)
            )
            if len(old_records) > 20:
                lines.append(f"\n... 共 {len(old_records)} 条，仅显示前 20 条")
            lines.append("\n回复本条消息序号可查看链接")
            await self._reply_records(
                event, "\n".join(lines), display_records, show_old_tag=True
            )
            return

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

    def _is_current_record(self, record: dict, category: str = "") -> bool:
        current_cats = record.get("_current_cats", set())
        if category:
            return any(category in c for c in current_cats)
        return bool(current_cats)

    def _split_current_records(
        self, records: list[dict], category: str = ""
    ) -> tuple[list[dict], list[dict]]:
        current = []
        old = []
        for r in records:
            if self._is_current_record(r, category):
                current.append(r)
            else:
                old.append(r)
        return current, old

    def _format_record_line(
        self,
        r: dict,
        index: int,
        category: str = "",
        show_old_tag: bool = False,
    ) -> str:
        team = json.loads(r["team_json"])
        names = ",".join(_team_member_name(t) for t in team[:5])
        cats = ",".join(json.loads(r["category_json"]))
        mode = self._record_difficulty_label(r)
        remark = r.get("remark1", "")
        remark_line = f"\n备注: {remark}" if remark else ""
        tag = ""
        if show_old_tag:
            tag = " [旧]"
        return (
            f"\n[{index}] {r['operation']} {r['cn_name']} {mode} [{cats}]{tag}\n"
            f"阵容: {names}\n"
            f"投稿: {r['raider']}"
            f"{remark_line}"
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

    def _resolve_category(self, kw: str) -> str:
        """分类名允许别名和省略常见后缀，如 特种 -> 特种队。"""
        alias = CATEGORY_ALIASES.get(kw) or CATEGORY_ALIASES.get(kw.lower())
        base = alias or kw
        candidates = [base]
        for suffix in CATEGORY_SUFFIXES:
            if not base.endswith(suffix):
                candidates.append(f"{base}{suffix}")
        for candidate in candidates:
            if self.db.query_records(category=candidate, limit=1):
                return candidate
        return ""

    # ====== 新旧分类 ======

    @staticmethod
    def _record_difficulty_key(record: dict) -> str:
        if record.get("grp") == "沙盘推演":
            return "sandbox"
        return record.get("operationType", "")

    @classmethod
    def _record_difficulty_label(cls, record: dict) -> str:
        difficulty = cls._record_difficulty_key(record)
        if difficulty == "sandbox":
            return "沙盘"
        if difficulty == "challenge":
            return "突袭"
        return ""

    @staticmethod
    def _mark_current(records: list[dict]) -> list[dict]:
        """同 (operation, category, difficulty) 下最少人(解手流最少步)标记为当前，其余为旧。
        每个记录附加 _current_cats 集合，表示该记录在哪些分类下是当前纪录。"""
        def parse_step(remark: str) -> int:
            m = re.search(r"(\d+)步", remark)
            return int(m.group(1)) if m else 99

        groups: dict[tuple, list[dict]] = {}
        for r in records:
            cats = json.loads(r.get("category_json", "[]"))
            difficulty = ArkRecPlugin._record_difficulty_key(r)
            for cat in cats:
                key = (r["operation"], cat, difficulty)
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
            difficulty = ArkRecPlugin._record_difficulty_key(r)
            r["_current_cats"] = {cat for cat in cats
                                   if r["_id"] in best_ids.get((r["operation"], cat, difficulty), set())}
        return records

    # ====== 独享纪录 ======

    @staticmethod
    def _exclusive_mode_key(operation: dict) -> str:
        return "challenge" if operation.get("operationType") == " 突袭" else "normal"

    @staticmethod
    def _exclusive_mode_label(mode: str) -> str:
        return "突袭" if mode == "challenge" else "普通"

    @staticmethod
    def _exclusive_match_operator(name: str, keyword: str) -> bool:
        if not keyword:
            return True
        return keyword.lower() in name.lower()

    @staticmethod
    def _exclusive_is_open_operation(operation: dict, open_episodes: set[str]) -> bool:
        story = operation.get("story", "")
        episode = operation.get("episode", "")
        return story in EXCLUSIVE_ALWAYS_OPEN_STORIES or episode in open_episodes

    def _filter_exclusive_ops(
        self,
        operations: list[dict],
        category: str = "",
        mode: str = "",
        open_episodes: set[str] | None = None,
        include_closed: bool = False,
    ) -> list[dict]:
        open_episodes = open_episodes or set()
        result = []
        for op in operations:
            if category and op.get("category") != category:
                continue
            if mode and self._exclusive_mode_key(op) != mode:
                continue
            if not include_closed and not self._exclusive_is_open_operation(op, open_episodes):
                continue
            result.append(op)
        result.sort(
            key=lambda op: (
                op.get("story", ""),
                op.get("episode", ""),
                op.get("operation", ""),
                op.get("operationType", ""),
                op.get("category", ""),
            )
        )
        return result

    def _exclusive_operator_rows(
        self,
        data: list[dict],
        operator: str,
        category: str = "",
        mode: str = "",
        limit: int = EXCLUSIVE_IMAGE_MAX_ROWS,
        open_episodes: set[str] | None = None,
        include_closed: bool = False,
    ) -> tuple[str, list[dict], int]:
        matches = [
            entry for entry in data
            if self._exclusive_match_operator(entry.get("name", ""), operator)
        ]
        if not matches:
            return "", [], 0
        exact = [entry for entry in matches if entry.get("name") == operator]
        entry = (exact or matches)[0]
        rows = self._filter_exclusive_ops(
            entry.get("operations", []), category, mode, open_episodes, include_closed
        )
        return entry.get("name", operator), rows[:limit], len(rows)

    def _exclusive_rank_rows(
        self,
        data: list[dict],
        category: str = "",
        mode: str = "",
        limit: int = EXCLUSIVE_IMAGE_MAX_ROWS,
        open_episodes: set[str] | None = None,
        include_closed: bool = False,
    ) -> list[dict]:
        rows = []
        for entry in data:
            ops = self._filter_exclusive_ops(
                entry.get("operations", []), category, mode, open_episodes, include_closed
            )
            if not ops:
                continue
            normal = sum(1 for op in ops if self._exclusive_mode_key(op) == "normal")
            challenge = len(ops) - normal
            rows.append({
                "name": entry.get("name", ""),
                "count": len(ops),
                "normal": normal,
                "challenge": challenge,
                "sample": ops[:3],
            })
        rows.sort(key=lambda row: (-row["count"], row["name"]))
        return rows[:limit]

    @staticmethod
    def _exclusive_image_html(
        title: str,
        subtitle: str,
        rows: list[dict],
        view: str,
        total_count: int = 0,
    ) -> str:
        def esc(value) -> str:
            return html.escape(str(value or ""), quote=True)

        body = []
        if view == "operator":
            for i, op in enumerate(rows, 1):
                mode = ArkRecPlugin._exclusive_mode_label(
                    ArkRecPlugin._exclusive_mode_key(op)
                )
                body.append(f"""
<section class="row op-row">
  <div class="idx">{i}</div>
  <div class="main">
    <div class="head">
      <span class="stage">{esc(op.get("operation", ""))}</span>
      <span class="stage-name">{esc(op.get("cn_name", ""))}</span>
      <span class="badge {esc(ArkRecPlugin._exclusive_mode_key(op))}">{esc(mode)}</span>
      <span class="chip">{esc(op.get("category", ""))}</span>
    </div>
    <div class="meta">{esc(op.get("story", ""))} / {esc(op.get("episode", ""))}</div>
  </div>
</section>""")
        else:
            for i, row in enumerate(rows, 1):
                samples = "、".join(
                    f'{op.get("operation", "")} {op.get("cn_name", "")}'
                    for op in row.get("sample", [])
                )
                body.append(f"""
<section class="row rank-row">
  <div class="idx">{i}</div>
  <div class="main">
    <div class="head">
      <span class="operator">{esc(row.get("name", ""))}</span>
      <span class="count">{esc(row.get("count", 0))} 条</span>
      <span class="badge normal">普通 {esc(row.get("normal", 0))}</span>
      <span class="badge challenge">突袭 {esc(row.get("challenge", 0))}</span>
    </div>
    <div class="meta">{esc(samples)}</div>
  </div>
</section>""")

        notes = []
        if total_count > len(rows):
            notes.append(f"共 {total_count} 条，仅显示前 {len(rows)} 条")
        notes.append("数据来源: wiki.arkrec.com/exclusive-records")
        note_html = "".join(f"<div>{esc(note)}</div>" for note in notes)

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  padding: 24px;
  background:
    linear-gradient(135deg, rgba(21, 128, 61, .11), rgba(37, 99, 235, .09) 45%, rgba(245, 158, 11, .08)),
    linear-gradient(rgba(255,255,255,.55) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,255,255,.55) 1px, transparent 1px),
    #eef3f7;
  background-size: auto, 18px 18px, 18px 18px, auto;
  color: #172033;
  font-family: "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", sans-serif;
}}
.card {{
  width: 820px;
  margin: 0 auto;
  background: rgba(255, 255, 255, .97);
  border: 1px solid rgba(210, 220, 230, .95);
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 18px 46px rgba(15, 23, 42, .16), 0 2px 8px rgba(15, 23, 42, .06);
}}
.title {{
  padding: 18px 22px 8px;
  background: linear-gradient(90deg, rgba(21, 128, 61, .10), rgba(37, 99, 235, .08)), #f8fafc;
  font-size: 24px;
  font-weight: 750;
  letter-spacing: 0;
}}
.subtitle {{
  padding: 0 22px 14px;
  border-bottom: 1px solid #e5eaf1;
  color: #64748b;
  font-size: 14px;
}}
.rows {{ padding: 10px 14px 6px; }}
.row {{
  display: grid;
  grid-template-columns: 36px 1fr;
  gap: 12px;
  padding: 11px 8px;
  border-bottom: 1px solid #edf0f5;
}}
.row:last-child {{ border-bottom: 0; }}
.idx {{
  width: 30px;
  height: 30px;
  border-radius: 6px;
  background: #15803d;
  color: #fff;
  font-size: 16px;
  font-weight: 700;
  line-height: 30px;
  text-align: center;
}}
.head {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-height: 30px; }}
.stage, .operator {{ font-size: 18px; font-weight: 760; color: #0f172a; }}
.stage-name {{ font-size: 17px; font-weight: 650; color: #1f2937; }}
.count {{
  padding: 2px 8px;
  border-radius: 5px;
  background: #ecfdf5;
  color: #047857;
  border: 1px solid #bbf7d0;
  font-size: 13px;
  font-weight: 700;
}}
.badge, .chip {{
  display: inline-block;
  border-radius: 5px;
  padding: 2px 7px;
  font-size: 13px;
  line-height: 18px;
  white-space: nowrap;
}}
.badge.normal {{ background: #eef6ff; color: #175da8; border: 1px solid #cfe4fb; }}
.badge.challenge {{ background: #fff1d6; color: #8a4b00; border: 1px solid #ffd58a; }}
.chip {{ background: #f1f5f9; color: #475569; border: 1px solid #d8e0ea; }}
.meta {{
  margin-top: 5px;
  color: #64748b;
  font-size: 14px;
  line-height: 1.45;
  word-break: break-word;
}}
.footer {{
  border-top: 1px solid #e5eaf1;
  background: #fbfcfe;
  padding: 12px 22px 16px;
  font-size: 14px;
  color: #64748b;
  line-height: 1.7;
}}
</style>
</head>
<body>
<div class="card">
  <div class="title">{esc(title)}</div>
  <div class="subtitle">{esc(subtitle)}</div>
  <div class="rows">{''.join(body)}</div>
  <div class="footer">{note_html}</div>
</div>
</body>
</html>"""

    async def _render_exclusive_image(
        self,
        title: str,
        subtitle: str,
        rows: list[dict],
        view: str,
        total_count: int = 0,
    ) -> Path | None:
        html_doc = self._exclusive_image_html(title, subtitle, rows, view, total_count)
        png_path = self.image_dir / f"arkrec_exclusive_{int(time.time() * 1000)}.png"
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page(viewport={"width": 868, "height": 900})
                await page.set_content(html_doc, wait_until="networkidle")
                card = await page.query_selector(".card")
                if card:
                    box = await card.bounding_box()
                    if box:
                        await page.set_viewport_size({
                            "width": 868,
                            "height": min(max(int(box["height"]) + 48, 360), 3200),
                        })
                await page.screenshot(path=str(png_path), full_page=True)
                await browser.close()
            return png_path
        except Exception as e:
            logger.error(f"ArkRec render exclusive image failed: {e}")
            return None

    async def _reply_exclusive_image(
        self,
        event: GroupMessageEvent,
        title: str,
        subtitle: str,
        rows: list[dict],
        view: str,
        total_count: int = 0,
    ):
        image_path = await self._render_exclusive_image(
            title, subtitle, rows, view, total_count
        )
        if image_path:
            try:
                await event.reply(f"[CQ:image,file={image_path.resolve().as_posix()}]")
                return
            except Exception as e:
                logger.error(f"ArkRec send exclusive image failed: {e}")
            finally:
                try:
                    image_path.unlink(missing_ok=True)
                except Exception:
                    pass
        await event.reply(f"{title}\n{subtitle}\n图片生成或发送失败")

    # ====== 关卡一览 ======

    @staticmethod
    def _brief_metric(row: dict, category: str, mode: str) -> dict | None:
        value = row.get(category)
        if not isinstance(value, dict):
            return None
        metric = value.get(mode)
        return metric if isinstance(metric, dict) else None

    @staticmethod
    def _brief_has_record(row: dict, category: str) -> bool:
        value = row.get(category)
        if not isinstance(value, dict):
            return False
        for mode in ("normal", "challenge"):
            metric = value.get(mode)
            if isinstance(metric, dict) and (metric.get("count") or 0) > 0:
                return True
        return False

    @staticmethod
    def _brief_current_episode_names(bundle_ext: dict, operations: list[dict]) -> set[str]:
        indexes = bundle_ext.get("currentEpisode") or []
        if not isinstance(indexes, list) or len(indexes) < 2:
            return set()
        story_index = indexes[0]
        episode_indexes = [indexes[1]]
        if len(indexes) >= 6 and indexes[4] >= 0 and indexes[5] >= 0:
            episode_indexes.append(indexes[5])

        stories = []
        for op in operations:
            story = op.get("story", "")
            episode = op.get("episode", "")
            if story and episode and story not in [item[0] for item in stories]:
                stories.append((story, []))
            if story and episode:
                for item_story, episodes in stories:
                    if item_story == story and episode not in episodes:
                        episodes.append(episode)
                        break
        if story_index < 0 or story_index >= len(stories):
            return set()
        current = set()
        episodes = stories[story_index][1]
        for episode_index in episode_indexes:
            if 0 <= episode_index < len(episodes):
                current.add(episodes[episode_index])
        return current

    def _brief_rows(
        self,
        operation_info: list[dict],
        operations: list[dict],
        bundle_ext: dict,
        category: str,
        scope: str,
        show_all: bool,
        empty_only: bool,
        limit: int,
    ) -> tuple[list[dict], int, str]:
        info_map = {
            (row.get("operation", ""), row.get("cn_name", "")): row
            for row in operation_info
        }
        current_episodes = self._brief_current_episode_names(bundle_ext, operations)
        selected = []
        for op in operations:
            if scope == "current" and op.get("episode") not in current_episodes:
                continue
            row = dict(info_map.get((op.get("operation", ""), op.get("cn_name", "")), {}))
            row.setdefault("story", op.get("story", ""))
            row.setdefault("episode", op.get("episode", ""))
            row.setdefault("operation", op.get("operation", ""))
            row.setdefault("cn_name", op.get("cn_name", ""))
            has_record = self._brief_has_record(row, category)
            if not show_all and not has_record:
                continue
            if empty_only and has_record:
                continue
            selected.append(row)
        return selected[:limit], len(selected), "、".join(sorted(current_episodes)) or "当前活动"

    @staticmethod
    def _brief_image_html(
        title: str,
        subtitle: str,
        rows: list[dict],
        category: str,
        total_count: int,
    ) -> str:
        def esc(value) -> str:
            return html.escape(str(value or ""), quote=True)

        body = []
        for i, row in enumerate(rows, 1):
            normal = ArkRecPlugin._brief_metric(row, category, "normal")
            challenge = ArkRecPlugin._brief_metric(row, category, "challenge")
            normal_num = normal.get("num") if normal else None
            normal_count = normal.get("count") if normal else 0
            challenge_num = challenge.get("num") if challenge else None
            challenge_count = challenge.get("count") if challenge else 0
            empty = not normal_count and not challenge_count
            body.append(f"""
<tr class="{ 'empty' if empty else '' }">
  <td class="idx">{i}</td>
  <td>
    <div class="stage"><span class="code">{esc(row.get("operation", ""))}</span><span>{esc(row.get("cn_name", ""))}</span></div>
    <div class="episode">{esc(row.get("episode", ""))}</div>
  </td>
  <td class="num">{esc(normal_num if normal_num is not None and normal_count else "-")}</td>
  <td class="count">{esc(normal_count or "-")}</td>
  <td class="num challenge">{esc(challenge_num if challenge_num is not None and challenge_count else "-")}</td>
  <td class="count">{esc(challenge_count or "-")}</td>
</tr>""")

        notes = []
        if total_count > len(rows):
            notes.append(f"共 {total_count} 关，仅显示前 {len(rows)} 关")
        notes.append("无纪录关卡会以灰色显示")
        note_html = "".join(f"<div>{esc(note)}</div>" for note in notes)

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  padding: 24px;
  background:
    linear-gradient(135deg, rgba(37, 99, 235, .10), rgba(20, 184, 166, .10) 48%, rgba(245, 158, 11, .07)),
    linear-gradient(rgba(255,255,255,.55) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,255,255,.55) 1px, transparent 1px),
    #eef3f7;
  background-size: auto, 18px 18px, 18px 18px, auto;
  color: #172033;
  font-family: "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", sans-serif;
}}
.card {{
  width: 860px;
  margin: 0 auto;
  background: rgba(255, 255, 255, .97);
  border: 1px solid rgba(210, 220, 230, .95);
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 18px 46px rgba(15, 23, 42, .16), 0 2px 8px rgba(15, 23, 42, .06);
}}
.title {{
  padding: 18px 22px 8px;
  background: linear-gradient(90deg, rgba(37, 99, 235, .10), rgba(20, 184, 166, .08)), #f8fafc;
  font-size: 24px;
  font-weight: 750;
}}
.subtitle {{
  padding: 0 22px 14px;
  border-bottom: 1px solid #e5eaf1;
  color: #64748b;
  font-size: 14px;
}}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
thead {{ background: #f1f5f9; color: #475569; }}
th {{ text-align: left; padding: 10px 12px; font-size: 12px; font-weight: 750; }}
td {{ padding: 10px 12px; border-top: 1px solid #edf0f5; vertical-align: middle; }}
.idx {{ width: 42px; color: #64748b; font-family: ui-monospace, Menlo, Consolas, monospace; }}
.stage {{ display: flex; align-items: baseline; gap: 8px; font-weight: 650; color: #0f172a; }}
.code {{ font-family: ui-monospace, Menlo, Consolas, monospace; color: #1d4ed8; font-weight: 760; }}
.episode {{ margin-top: 3px; color: #64748b; font-size: 12px; }}
.num, .count {{ text-align: right; font-family: ui-monospace, Menlo, Consolas, monospace; }}
.num {{ color: #0f172a; font-weight: 760; }}
.challenge {{ color: #8a4b00; }}
.empty {{ color: #94a3b8; background: #fbfcfe; }}
.empty .stage, .empty .code, .empty .num {{ color: #94a3b8; }}
.footer {{
  border-top: 1px solid #e5eaf1;
  background: #fbfcfe;
  padding: 12px 22px 16px;
  font-size: 14px;
  color: #64748b;
  line-height: 1.7;
}}
</style>
</head>
<body>
<div class="card">
  <div class="title">{esc(title)}</div>
  <div class="subtitle">{esc(subtitle)}</div>
  <table>
    <thead>
      <tr><th>#</th><th>关卡</th><th>普通最低</th><th>普通记录</th><th>突袭最低</th><th>突袭记录</th></tr>
    </thead>
    <tbody>{''.join(body)}</tbody>
  </table>
  <div class="footer">{note_html}</div>
</div>
</body>
</html>"""

    async def _render_brief_image(
        self,
        title: str,
        subtitle: str,
        rows: list[dict],
        category: str,
        total_count: int,
    ) -> Path | None:
        html_doc = self._brief_image_html(title, subtitle, rows, category, total_count)
        png_path = self.image_dir / f"arkrec_brief_{int(time.time() * 1000)}.png"
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page(viewport={"width": 908, "height": 900})
                await page.set_content(html_doc, wait_until="networkidle")
                card = await page.query_selector(".card")
                if card:
                    box = await card.bounding_box()
                    if box:
                        await page.set_viewport_size({
                            "width": 908,
                            "height": min(max(int(box["height"]) + 48, 360), 3400),
                        })
                await page.screenshot(path=str(png_path), full_page=True)
                await browser.close()
            return png_path
        except Exception as e:
            logger.error(f"ArkRec render brief image failed: {e}")
            return None

    async def _reply_brief_image(
        self,
        event: GroupMessageEvent,
        title: str,
        subtitle: str,
        rows: list[dict],
        category: str,
        total_count: int,
    ):
        image_path = await self._render_brief_image(
            title, subtitle, rows, category, total_count
        )
        if image_path:
            try:
                await event.reply(f"[CQ:image,file={image_path.resolve().as_posix()}]")
                return
            except Exception as e:
                logger.error(f"ArkRec send brief image failed: {e}")
            finally:
                try:
                    image_path.unlink(missing_ok=True)
                except Exception:
                    pass
        await event.reply(f"{title}\n{subtitle}\n图片生成或发送失败")

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
                current_records, old_records = self._split_current_records(records, category)
                lines = [f"最近常规队当前纪录: {len(current_records)} 条"]
                display_records = current_records[:20]
                lines.extend(
                    self._format_record_line(r, i, category)
                    for i, r in enumerate(display_records, 1)
                )
                if len(current_records) > 20:
                    lines.append(f"\n... 当前纪录共 {len(current_records)} 条，仅显示前 20 条")
                if old_records:
                    lines.append(f"\n旧纪录 {len(old_records)} 条，回复本条消息“旧”可查看")
                if display_records:
                    lines.append("\n回复本条消息序号可查看链接")
                await self._reply_records(
                    event, "\n".join(lines), display_records, old_records=old_records
                )
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
                elif not category and (resolved_category := self._resolve_category(kw)):
                    category = resolved_category
            elif not category and (resolved_category := self._resolve_category(kw)):
                category = resolved_category
            elif not operator and self.db.query_records(operator=kw, limit=1):
                operator = kw
            elif not operation:
                operation = self._resolve_operation(kw)

        # 无分类筛选时默认常规队
        if not category and not operator:
            category = "常规队"

        records = self.db.query_records(
            operator=operator, operation=operation, category=category, mode=mode, grp=grp, limit=200)
        if mode == "challenge" and not grp:
            records = [r for r in records if r.get("grp") != "沙盘推演"]

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

        current_records, old_records = self._split_current_records(records, category)
        lines = [f'"{filters}" 当前纪录 {len(current_records)} 条']
        display_records = current_records[:20]
        lines.extend(
            self._format_record_line(r, i, category)
            for i, r in enumerate(display_records, 1)
        )
        if len(current_records) > 20:
            lines.append(f"\n... 当前纪录共 {len(current_records)} 条，仅显示前 20 条")
        if old_records:
            lines.append(f"\n旧纪录 {len(old_records)} 条，回复本条消息“旧”可查看")
        if display_records:
            lines.append("\n回复本条消息序号可查看链接")

        await self._reply_records(
            event, "\n".join(lines), display_records, old_records=old_records
        )

    @command_registry.command("arkrec_top", description="最近 N 条记录")
    @param(name="count", default="20", help="数量")
    @param(name="category", default="", help="筛选分类")
    async def cmd_top(self, event: GroupMessageEvent, count: str = "20",
                      category: str = ""):
        try:
            n = max(1, min(int(count), 50))
        except ValueError:
            n = 20
            if not category:
                category = count
        if category:
            category = self._resolve_category(category) or category
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

    @command_registry.command("arkrec_exclusive", description="查询独享纪录: [干员] [流派] [普通/突袭] [数量]")
    @param(name="p1", default="", help="干员名 / 流派 / 数量")
    @param(name="p2", default="", help="可选")
    @param(name="p3", default="", help="可选")
    @param(name="p4", default="", help="可选")
    async def cmd_exclusive(self, event: GroupMessageEvent, p1: str = "",
                            p2: str = "", p3: str = "", p4: str = ""):
        logger.info(
            f"arkrec exclusive: p1={p1!r} p2={p2!r} p3={p3!r} p4={p4!r}"
        )
        parts = [p.strip() for p in [p1, p2, p3, p4] if p.strip()]
        operator = ""
        category = "常规队"
        mode = ""
        limit = EXCLUSIVE_IMAGE_MAX_ROWS
        force = False
        include_closed = False

        for kw in parts:
            if kw in ("刷新", "refresh"):
                force = True
            elif kw in ("已关闭", "关闭", "closed", "show_closed"):
                include_closed = True
            elif kw in ("突袭", "challenge", "磨难", "险地", "磨难险地"):
                mode = "challenge"
            elif kw in ("普通", "normal", "标准"):
                mode = "normal"
            elif kw.isdigit():
                limit = max(1, min(int(kw), 50))
            elif resolved_category := self._resolve_category(kw):
                category = resolved_category
            elif kw in ("全部", "全流派", "所有流派", "all"):
                category = ""
            elif not operator:
                operator = kw

        try:
            data = await self._get_exclusive_operators(force=force)
            open_episodes = await self._get_open_episodes(force=force)
        except Exception as e:
            logger.error(f"ArkRec fetch exclusive failed: {e}")
            await event.reply(f"独享纪录获取失败: {e}")
            return

        mode_label = self._exclusive_mode_label(mode) if mode else "普通+突袭"
        category_label = category or "全流派"
        closed_label = "含已关闭活动" if include_closed else "不含已关闭活动"
        if operator:
            display_name, rows, total = self._exclusive_operator_rows(
                data,
                operator,
                category,
                mode,
                limit,
                open_episodes,
                include_closed,
            )
            if not display_name:
                await event.reply(f'未找到干员 "{operator}" 的独享纪录')
                return
            if not rows:
                await event.reply(f"{display_name} 在 {category_label} / {mode_label} / {closed_label} 下暂无独享纪录")
                return
            await self._reply_exclusive_image(
                event,
                f"{display_name} 独享纪录",
                f"筛选: {category_label} / {mode_label} / {closed_label}",
                rows,
                "operator",
                total_count=total,
            )
            return

        rows = self._exclusive_rank_rows(
            data, category, mode, limit, open_episodes, include_closed
        )
        if not rows:
            await event.reply(f"{category_label} / {mode_label} / {closed_label} 下暂无独享纪录")
            return
        total = sum(1 for entry in data if self._filter_exclusive_ops(
            entry.get("operations", []), category, mode, open_episodes, include_closed
        ))
        await self._reply_exclusive_image(
            event,
            "独享纪录排行",
            f"筛选: {category_label} / {mode_label} / {closed_label}",
            rows,
            "rank",
            total_count=total,
        )

    @command_registry.command("arkrec_brief", description="关卡一览: 默认当前活动常规队，显示无纪录关卡")
    @param(name="p1", default="", help="流派 / 数量 / 刷新")
    @param(name="p2", default="", help="可选")
    @param(name="p3", default="", help="可选")
    @param(name="p4", default="", help="可选")
    async def cmd_brief(self, event: GroupMessageEvent, p1: str = "",
                        p2: str = "", p3: str = "", p4: str = ""):
        logger.info(f"arkrec brief: p1={p1!r} p2={p2!r} p3={p3!r} p4={p4!r}")
        parts = [p.strip() for p in [p1, p2, p3, p4] if p.strip()]
        category = "常规队"
        limit = BRIEF_IMAGE_MAX_ROWS
        force = False
        show_all = True
        empty_only = False
        scope = "current"

        for kw in parts:
            if kw in ("刷新", "refresh"):
                force = True
            elif kw.isdigit():
                limit = max(1, min(int(kw), 120))
            elif kw in ("仅无记录", "仅无纪录", "无记录", "无纪录", "empty"):
                empty_only = True
                show_all = True
            elif kw in ("有记录", "有纪录", "hide_empty"):
                show_all = False
                empty_only = False
            elif kw in ("全部关卡", "全部", "all"):
                scope = "all"
            elif resolved_category := self._resolve_category(kw):
                category = resolved_category

        try:
            operation_info = await self._get_operation_info(force=force)
            bundle_ext = await self._get_bundle_ext(force=force)
            operations = await self._get_operation_rows_from_menu(force=force)
        except Exception as e:
            logger.error(f"ArkRec fetch brief failed: {e}")
            await event.reply(f"关卡一览获取失败: {e}")
            return

        rows, total, current_name = self._brief_rows(
            operation_info,
            operations,
            bundle_ext,
            category,
            scope,
            show_all,
            empty_only,
            limit,
        )
        if not rows:
            await event.reply(f"{category} / {current_name} 下没有符合条件的关卡")
            return
        scope_label = "全部关卡" if scope == "all" else f"当前活动: {current_name}"
        empty_label = "仅无纪录关卡" if empty_only else ("含无纪录关卡" if show_all else "仅有纪录关卡")
        await self._reply_brief_image(
            event,
            "关卡一览",
            f"筛选: {scope_label} / {category} / {empty_label}",
            rows,
            category,
            total_count=total,
        )

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
        elif resolved_category := self._resolve_category(val):
            if resolved_category not in sub.categories:
                sub.categories.append(resolved_category)
            await event.reply(f"已订阅分类: {resolved_category}")
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
        resolved_category = self._resolve_category(val)
        candidates = {val}
        if resolved_category:
            candidates.add(resolved_category)
        for lst in [sub.categories, sub.operators, sub.operations]:
            for candidate in list(candidates):
                if candidate in lst:
                    lst.remove(candidate)
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
