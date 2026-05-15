"""arkrec wiki 认证模块 —— 登录、session 持久化、自动续期"""

import hashlib
import json
from pathlib import Path

import httpx
from ncatbot.utils import get_log

logger = get_log("ArkRec")

AUTH_URL = "https://wiki.arkrec.com/v1"


class ArkRecAuth:
    """管理 wiki.arkrec.com 的登录会话"""

    def __init__(self, data_dir: Path, email: str, password: str):
        self._email = email
        self._pw_hash = hashlib.sha256(password.encode()).hexdigest()
        self._cookie_path = data_dir / "session.json"
        self._client: httpx.AsyncClient | None = None

    # ====== 登录 ======

    async def _do_login(self) -> dict:
        """执行登录，返回 cookies dict"""
        resp = await self._raw_client().post(
            f"{AUTH_URL}/authentication/login",
            json={"username": self._email, "password": self._pw_hash, "checked": False},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"登录失败 HTTP {resp.status_code}: {resp.text[:200]}")
        cookies = dict(resp.cookies.items())
        # httpx 的 resp.cookies 可能不完整，手动补充真正的 cookie（不含属性）
        for k_bytes, v_bytes in resp.headers.raw:
            if k_bytes.lower() == b"set-cookie":
                first_part = v_bytes.decode("latin-1").split(";")[0].strip()
                if "=" in first_part:
                    key, val = first_part.split("=", 1)
                    cookies[key] = val
        return cookies

    async def _validate(self, cookies: dict) -> bool:
        """检查 cookies 是否还有效"""
        try:
            client = self._raw_client()
            resp = await client.get(
                f"{AUTH_URL}/authentication/get-user",
                cookies=cookies,
            )
            return resp.status_code == 200
        except Exception:
            return False

    # ====== 公共接口 ======

    async def get_client(self) -> httpx.AsyncClient:
        """返回已认证的 httpx AsyncClient，自动处理登录/续期"""
        if self._client is not None:
            return self._client

        cookies = self._load_cookies()
        if not cookies or not await self._validate(cookies):
            logger.info("登录 arkrec wiki...")
            cookies = await self._do_login()
            self._save_cookies(cookies)
            logger.info("arkrec 登录成功")

        self._client = httpx.AsyncClient(
            cookies=cookies,
            headers={"User-Agent": "37Bot-ArkRec/1.0"},
            timeout=30,
        )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    def _raw_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"User-Agent": "37Bot-ArkRec/1.0"},
            timeout=30,
        )

    # ====== 持久化 ======

    def _load_cookies(self) -> dict | None:
        if not self._cookie_path.exists():
            return None
        try:
            return json.loads(self._cookie_path.read_text("utf-8"))
        except Exception:
            return None

    def _save_cookies(self, cookies: dict):
        self._cookie_path.parent.mkdir(parents=True, exist_ok=True)
        self._cookie_path.write_text(
            json.dumps(cookies, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
