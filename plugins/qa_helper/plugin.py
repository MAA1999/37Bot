"""项目知识库 Q&A 插件"""

import asyncio
import json
import re

import httpx

from ncatbot.plugin_system import NcatBotPlugin, command_registry, param, on_message
from ncatbot.core.event import GroupMessageEvent, PrivateMessageEvent
from ncatbot.utils import get_log, ncatbot_config

from plugins._ai import get_llm, is_llm_configured, load_llm_config, save_llm_config
from .config import QAGroupConfig

logger = get_log("QA")

DOCS_URLS: dict[str, list[str]] = {
    "m9a": [
        "https://raw.githubusercontent.com/MAA1999/M9A/main/docs/zh_cn/manual/faq.md",
        "https://raw.githubusercontent.com/MAA1999/M9A/main/docs/zh_cn/manual/introduction.md",
        "https://raw.githubusercontent.com/MAA1999/M9A/main/docs/zh_cn/manual/newbie.md",
        "https://raw.githubusercontent.com/MAA1999/M9A/main/docs/zh_cn/manual/connection.md",
        "https://raw.githubusercontent.com/MAA1999/M9A/main/docs/zh_cn/manual/cli.md",
        "https://raw.githubusercontent.com/MAA1999/M9A/main/docs/zh_cn/manual/MirrorChyan.md",
    ],
    "maaend": [
        "https://raw.githubusercontent.com/MaaEnd/MaaEnd/v2/README.md",
        "https://raw.githubusercontent.com/MaaEnd/MaaEnd/v2/docs/zh_cn/users/troubleshooting.md",
    ],
    "mxu": [
        "https://raw.githubusercontent.com/MistEO/MXU/main/README.md",
        "https://raw.githubusercontent.com/MistEO/MXU/main/docs/add-special-task.md",
    ],
    "mfaa": [
        "https://raw.githubusercontent.com/SweetSmellFox/MFAAvalonia/master/README.md",
        "https://raw.githubusercontent.com/SweetSmellFox/MFAAvalonia/master/docs/zh/外部通知.md",
        "https://raw.githubusercontent.com/SweetSmellFox/MFAAvalonia/master/docs/zh/自定义布局.md",
    ],
}

ISSUES_REPOS: dict[str, str] = {
    "m9a": "MAA1999/M9A",
    "maaend": "MaaEnd/MaaEnd",
    "mxu": "MistEO/MXU",
    "mfaa": "SweetSmellFox/MFAAvalonia",
}

PROJECT_ALIASES: dict[str, str] = {
    "mfa": "mfaa",
    "mfaavalonia": "mfaa",
}

def _normalize_project(name: str) -> str:
    return PROJECT_ALIASES.get(name.lower(), name.lower())


class QaHelperPlugin(NcatBotPlugin):
    name = "QaHelperPlugin"
    version = "1.0.0"
    author = "Windsland52"
    dependencies = {}

    async def on_load(self):
        self.config_path = self.workspace / "config.json"
        self.groups: dict[str, QAGroupConfig] = self._load_config()
        self._bot_qq: str | None = None
        self._name_cache: dict[str, str] = {}
        asyncio.create_task(self._auto_refresh_loop())

    async def _auto_refresh_loop(self):
        await asyncio.sleep(30)  # 等待插件初始化完成
        while True:
            await asyncio.sleep(1800)
            await self._refresh_all()

    async def _refresh_all(self):
        projects = set()
        for cfg in self.groups.values():
            if cfg.enabled:
                projects.update(p.lower() for p in cfg.projects)
        for project in projects:
            try:
                prompt = await self._fetch_docs(project)
                if prompt:
                    cache_path = self.workspace / f"cache_{project}.txt"
                    cache_path.write_text(prompt, encoding="utf-8")
                    size_kb = len(prompt.encode("utf-8")) / 1024
                    logger.info(f"自动刷新 {project} 完成 ({size_kb:.0f}KB)")
                else:
                    logger.warning(f"自动刷新 {project} 失败")
            except Exception as e:
                logger.error(f"自动刷新 {project} 异常: {e}")

    def _load_config(self) -> dict[str, QAGroupConfig]:
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text("utf-8"))
                result = {}
                for gid, g in data.items():
                    proj = g.get("projects")
                    if proj:
                        projects = [p for p in proj if isinstance(p, str)]
                    elif g.get("project"):
                        projects = [_normalize_project(g["project"])]
                    else:
                        projects = []
                    result[gid] = QAGroupConfig(
                        enabled=g.get("enabled", False),
                        projects=projects,
                        system_prompt=g.get("system_prompt", ""),
                    )
                return result
            except Exception:
                pass
        return {}

    def _save_config(self):
        self.config_path.write_text(
            json.dumps(
                {
                    gid: {
                        "enabled": g.enabled,
                        "projects": g.projects,
                        "system_prompt": g.system_prompt,
                    }
                    for gid, g in self.groups.items()
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    async def _is_group_admin(self, group_id: str, user_id: str) -> bool:
        try:
            info = await self.api.get_group_member_info(group_id, user_id)
            return info.role in ("owner", "admin")
        except Exception as e:
            logger.error(f"get_group_member_info error: {e}")
            return False

    async def _get_bot_qq(self) -> str:
        if self._bot_qq is None:
            self._bot_qq = str(ncatbot_config.bt_uin)
        return self._bot_qq

    async def _resolve_user_name(self, group_id: str, user_id: str) -> str:
        cache_key = f"{group_id}_{user_id}"
        if cache_key in self._name_cache:
            return self._name_cache[cache_key]
        try:
            info = await self.api.get_group_member_info(group_id, user_id)
            name = info.card or info.nickname or user_id
        except Exception:
            name = user_id
        display = f"{name}({user_id})"
        self._name_cache[cache_key] = display
        return display

    # ====== GitHub Token ======

    def _load_gh_token(self) -> str:
        p = self.workspace / "github_token.txt"
        if p.exists():
            try:
                return p.read_text("utf-8").strip()
            except Exception:
                pass
        return ""

    def _save_gh_token(self, token: str):
        (self.workspace / "github_token.txt").write_text(
            token.strip(), encoding="utf-8"
        )

    def _gh_headers(self) -> dict:
        h = {"User-Agent": "37Bot-QA", "Accept": "application/vnd.github.v3+json"}
        token = self._load_gh_token()
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    # ====== 提示词 ======

    def _get_system_prompt(self, cfg: QAGroupConfig) -> str:
        if cfg.system_prompt:
            return cfg.system_prompt
        parts = []
        for p in cfg.projects:
            cache_path = self.workspace / f"cache_{p.lower()}.txt"
            if cache_path.exists():
                try:
                    parts.append(cache_path.read_text("utf-8").strip())
                except Exception:
                    pass
        return "\n\n=====\n\n".join(parts) if parts else ""

    @staticmethod
    def _strip_frontmatter(text: str) -> str:
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                text = text[end + 3 :].lstrip("\n")
        return text

    async def _fetch_docs(self, project: str) -> str | None:
        urls = DOCS_URLS.get(project.lower(), [])
        if not urls:
            return None

        async def fetch_one(url: str) -> str | None:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        return resp.text
                    logger.error(f"抓取失败 {url}: HTTP {resp.status_code}")
            except Exception as e:
                logger.error(f"抓取失败 {url}: {e}")
            return None

        results = await asyncio.gather(*[fetch_one(u) for u in urls])
        contents = [self._strip_frontmatter(r) for r in results if r]
        if not contents:
            return None

        combined = "\n\n---\n\n".join(contents)
        issue_section = await self._fetch_issues(project)
        if issue_section:
            combined += "\n\n---\n\n" + issue_section
        release_section = await self._fetch_releases(project)
        if release_section:
            combined += "\n\n---\n\n" + release_section
        return (
            f"你是 {project} 项目助手。请根据以下文档内容回答用户的问题。\n"
            f"回答应简洁准确。对于不知道的问题，直接说不知道，不要编造。\n\n"
            f"---\n\n"
            f"{combined}"
        )

    async def _fetch_issues(self, project: str) -> str | None:
        repo = ISSUES_REPOS.get(project.lower(), "")
        if not repo:
            return None

        headers = self._gh_headers()

        async def fetch_page(state: str, per_page: int) -> list[dict]:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(
                        f"https://api.github.com/repos/{repo}/issues",
                        params={
                            "state": state,
                            "sort": "updated",
                            "direction": "desc",
                            "per_page": per_page,
                            "filter": "all",
                        },
                        headers=headers,
                    )
                    if resp.status_code != 200:
                        logger.error(f"GitHub Issues API 失败: HTTP {resp.status_code}")
                        return []
                    return resp.json()
            except Exception as e:
                logger.error(f"GitHub Issues API 异常: {e}")
                return []

        open_issues = await fetch_page("open", 20)
        closed_issues = await fetch_page("closed", 10)

        def format_issues(issues: list[dict], state_label: str) -> str:
            lines = [f"### {state_label}"]
            count = 0
            for iss in issues:
                if "pull_request" in iss:
                    continue
                number = iss.get("number", "?")
                title = iss.get("title", "")
                labels = [l["name"] for l in iss.get("labels", [])]
                label_str = f" [{', '.join(labels)}]" if labels else ""
                lines.append(f"- #{number}{label_str}: {title}")
                count += 1
                if count >= 15:
                    break
            return "\n".join(lines)

        open_text = format_issues(open_issues, "Open Issues")
        closed_text = format_issues(closed_issues, "Recently Closed")
        combined = f"## GitHub Issues ({repo})\n\n{open_text}\n\n{closed_text}"
        return combined

    async def _fetch_releases(self, project: str) -> str | None:
        repo = ISSUES_REPOS.get(project.lower(), "")
        if not repo:
            return None

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo}/releases",
                    params={"per_page": 5},
                    headers=self._gh_headers(),
                )
                if resp.status_code != 200:
                    logger.error(f"GitHub Releases API 失败: HTTP {resp.status_code}")
                    return None
                releases = resp.json()
        except Exception as e:
            logger.error(f"GitHub Releases API 异常: {e}")
            return None

        if not releases:
            return None

        lines = [f"## Recent Releases ({repo})"]
        for rel in releases:
            tag = rel.get("tag_name", "?")
            name = rel.get("name") or tag
            published = (rel.get("published_at") or "")[:10]
            body = (rel.get("body") or "").strip()
            header = f"### {name} ({published})"
            if body:
                body_short = body[:600]
                if len(body) > 600:
                    body_short += "\n...(truncated)"
                header += "\n" + body_short
            lines.append(header)
        return "\n\n".join(lines)

    # ====== Q&A 处理 ======

    @staticmethod
    def _looks_like_question(text: str) -> bool:
        indicators = [
            "怎么",
            "如何",
            "为什么",
            "能不能",
            "可以不",
            "有没有",
            "是什么",
            "是谁",
            "在哪",
            "怎么办",
            "请问",
            "问一下",
            "请教",
            "求助",
            "帮我看",
            "帮我",
        ]
        return any(i in text for i in indicators)

    @on_message
    async def _on_message(self, event):
        if not isinstance(event, GroupMessageEvent):
            return

        group_id = str(event.group_id)
        cfg = self.groups.get(group_id)
        if not cfg or not cfg.enabled:
            return

        text = (event.raw_message or "").strip()
        if not text or text.startswith("/"):
            return

        # @bot / @others 检查
        bot_qq = await self._get_bot_qq()
        is_at_bot = False
        has_at_others = False
        for seg in event.message:
            if getattr(seg, "msg_seg_type", None) == "at":
                qq = str(getattr(seg, "qq", "") or getattr(seg, "user_id", ""))
                if qq == bot_qq:
                    is_at_bot = True
                elif qq:
                    has_at_others = True

        # 拉取上下文（@bot 与非 @bot 都带）
        ctx = ""
        try:
            recent = await self.api.get_group_msg_history(group_id, count=6)
            prev = [
                m
                for m in recent
                if m.time < event.time and m.message_id != event.message_id
            ]
            if prev:
                lines = []
                for m in reversed(prev[-3:]):
                    name = await self._resolve_user_name(group_id, str(m.user_id))
                    lines.append(f"[{name}]: {m.raw_message}")
                ctx = "\n".join(lines)
        except Exception:
            pass

        sender_name = await self._resolve_user_name(group_id, str(event.user_id))
        question = self._clean_question(event)

        if not is_at_bot:
            if has_at_others:
                return
            if not question or not self._looks_like_question(question):
                return
            if not is_llm_configured():
                return
            if not await get_llm().judge_question("、".join(cfg.projects), question, ctx):
                return
            logger.info(
                f"QA 触发（LLM判定）: group={group_id}, user={sender_name}, question={question[:100]}"
            )

        if not question:
            await event.reply("请问具体问题是什么？")
            return

        if not is_llm_configured():
            await event.reply("LLM 尚未配置，请联系管理员。")
            return

        full_question = f"提问者: {sender_name}\n{question}"
        system_prompt = self._get_system_prompt(cfg)
        answer = await get_llm().answer_question(full_question, system_prompt, ctx)
        if answer:
            await event.reply(answer)
        else:
            await event.reply("抱歉，暂时无法回答这个问题。")

    def _clean_question(self, event: GroupMessageEvent) -> str:
        parts = []
        for seg in event.message:
            seg_type = getattr(seg, "msg_seg_type", None)
            if seg_type in ("text", "plain"):
                t = getattr(seg, "text", "") or ""
                if t:
                    parts.append(t)
        text = " ".join(parts).strip()
        text = re.sub(r"^/\S+\s*", "", text)
        return text.strip()

    # ====== 管理命令 ======

    def _get_cfg(self, group_id: str) -> QAGroupConfig:
        if group_id not in self.groups:
            self.groups[group_id] = QAGroupConfig()
        return self.groups[group_id]

    @command_registry.command(
        "qa_llm", description="[root] 配置 LLM API（私聊，全局共享）"
    )
    async def cmd_llm(
        self, event: PrivateMessageEvent, base_url: str, api_key: str, model: str
    ):
        if event.message_type != "private":
            await event.reply("请私聊使用此命令")
            return
        if not self.rbac_manager.user_has_role(str(event.user_id), "root"):
            await event.reply("需要 root 权限")
            return
        cfg = load_llm_config()
        cfg.base_url = base_url.rstrip("/")
        cfg.api_key = api_key
        cfg.model = model
        save_llm_config(cfg)
        await event.reply(f"LLM 配置已更新: {model} @ {base_url}")

    @command_registry.command(
        "qa_ghtoken", description="[root] 配置 GitHub Token（私聊，提高 API 额度）"
    )
    async def cmd_ghtoken(self, event: PrivateMessageEvent, token: str = ""):
        if event.message_type != "private":
            await event.reply("请私聊使用此命令")
            return
        if not self.rbac_manager.user_has_role(str(event.user_id), "root"):
            await event.reply("需要 root 权限")
            return
        if not token:
            self._save_gh_token("")
            await event.reply("GitHub Token 已清除")
        else:
            self._save_gh_token(token)
            await event.reply("GitHub Token 已更新")

    @command_registry.command("qa", description="[管理员] Q&A 问答 on/off [项目名]")
    @param(name="action", default="on", help="on 或 off")
    @param(name="project", default="", help="项目名: M9A MaaEnd MXU MFAAvalonia")
    async def cmd_enable(
        self, event: GroupMessageEvent, action: str = "on", project: str = ""
    ):
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要群主或管理员权限")
            return
        group_id = str(event.group_id)
        cfg = self._get_cfg(group_id)
        if action.lower() == "on":
            if not project:
                await event.reply("请指定项目名，如 /qa on M9A")
                return
            p_lower = _normalize_project(project)
            if p_lower not in DOCS_URLS:
                await event.reply(f"未知项目: {project}，可用: {', '.join(DOCS_URLS)}")
                return
            if p_lower in (p.lower() for p in cfg.projects):
                await event.reply(f"{project} 已添加，当前项目: {', '.join(cfg.projects)}")
                return
            cfg.projects.append(p_lower)
            cfg.enabled = True
            self._save_config()
            await event.reply(f"Q&A 已启用: {', '.join(cfg.projects)}，正在抓取 {project}...")
            prompt = await self._fetch_docs(p_lower)
            if prompt:
                cache_path = self.workspace / f"cache_{p_lower}.txt"
                cache_path.write_text(prompt, encoding="utf-8")
                size_kb = len(prompt.encode("utf-8")) / 1024
                await event.reply(f"{project} 文档抓取完成 ({size_kb:.0f}KB)，Q&A 就绪")
            else:
                await event.reply(f"{project} 文档抓取失败，请稍后 /qa_refresh 重试")
        elif project:
            p_lower = _normalize_project(project)
            if p_lower in (p.lower() for p in cfg.projects):
                cfg.projects = [p for p in cfg.projects if p.lower() != p_lower]
                if not cfg.projects:
                    cfg.enabled = False
                self._save_config()
                if cfg.enabled:
                    await event.reply(f"已移除 {project}，当前项目: {', '.join(cfg.projects)}")
                else:
                    await event.reply(f"已移除 {project}，项目列表为空，Q&A 已禁用")
            else:
                await event.reply(f"项目中不存在 {project}")
        else:
            cfg.enabled = False
            cfg.projects = []
            self._save_config()
            await event.reply("Q&A 已禁用")

    @command_registry.command("qa_refresh", description="[管理员] 重新抓取项目文档")
    async def cmd_refresh(self, event: GroupMessageEvent):
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要群主或管理员权限")
            return
        group_id = str(event.group_id)
        cfg = self.groups.get(group_id)
        if not cfg or not cfg.projects:
            await event.reply("本群未启用 Q&A")
            return
        projects = ", ".join(cfg.projects)
        await event.reply(f"正在重新抓取 {projects} 文档...")
        results = []
        for p in cfg.projects:
            prompt = await self._fetch_docs(p)
            if prompt:
                cache_path = self.workspace / f"cache_{p}.txt"
                cache_path.write_text(prompt, encoding="utf-8")
                results.append(f"{p} OK")
            else:
                results.append(f"{p} 失败")
        await event.reply("\n".join(results))

    @command_registry.command("qa_prompt", description="[管理员] 设置/清除自定义提示词")
    @param(name="prompt", default="", help="系统提示词，留空清除（回退到文档缓存）")
    async def cmd_prompt(self, event: GroupMessageEvent, prompt: str = ""):
        if not await self._is_group_admin(event.group_id, event.user_id):
            await event.reply("需要群主或管理员权限")
            return
        group_id = str(event.group_id)
        cfg = self._get_cfg(group_id)
        if not prompt:
            cfg.system_prompt = ""
            self._save_config()
            if any(
                (self.workspace / f"cache_{p}.txt").exists()
                for p in cfg.projects
            ):
                await event.reply("提示词已清除，将使用文档缓存")
            else:
                await event.reply("提示词已清除（无回退源，请 /qa_refresh）")
        else:
            cfg.system_prompt = prompt
            self._save_config()
            await event.reply(f"自定义提示词已设置 ({len(prompt)} 字符)")

    @command_registry.command("qa_status", description="查看本群 Q&A 配置")
    async def cmd_status(self, event: GroupMessageEvent):
        group_id = str(event.group_id)
        cfg = self.groups.get(group_id)
        lines = [
            "Q&A 问答:",
            f"  状态: {'启用' if cfg and cfg.enabled else '禁用'}",
        ]
        if cfg and cfg.enabled:
            lines.append(f"  项目: {', '.join(cfg.projects) if cfg.projects else '无'}")
            if cfg.system_prompt:
                lines.append(f"  提示词: 自定义 ({len(cfg.system_prompt)} 字符)")
            else:
                caches = []
                for p in cfg.projects:
                    cp = self.workspace / f"cache_{p}.txt"
                    if cp.exists():
                        size = len(cp.read_text("utf-8").encode("utf-8")) / 1024
                        caches.append(f"{p}({size:.0f}KB)")
                if caches:
                    lines.append(f"  文档缓存: {', '.join(caches)}")
                else:
                    lines.append(f"  文档缓存: 未抓取")
        llm_cfg = load_llm_config()
        lines.append(f"LLM: {'已配置' if llm_cfg.base_url else '未配置'}")
        lines.append(f"GitHub Token: {'已配置' if self._load_gh_token() else '未配置'}")
        await event.reply("\n".join(lines))


__all__ = ["QaHelperPlugin"]
