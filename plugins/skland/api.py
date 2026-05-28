"""森空岛签到 API 客户端。"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

APP_CODE = "4ca99fa6b56cc2ba"
AS_BASE = "https://as.hypergryph.com"
SKLAND_BASE = "https://zonai.skland.com"
USER_AGENT = (
    "Skland/1.0.1 (com.hypergryph.skland; build:100001014; Android 31; ) "
    "Okhttp/4.11.0"
)


class SklandAuthError(RuntimeError):
    """登录态或长期 token 不可用。"""


@dataclass
class Credential:
    token: str
    cred: str


@dataclass
class Binding:
    app_code: str
    game_id: int
    uid: str
    nickname: str
    channel_name: str
    game_name: str = ""
    roles: list[dict] = field(default_factory=list)


@dataclass
class SignResult:
    success: bool
    game_name: str
    nickname: str
    channel_name: str
    awards: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def already_signed(self) -> bool:
        text = self.error.lower()
        return any(key in text for key in ("已签到", "重复", "今日已", "already"))


class SklandClient:
    def __init__(self, device_id: str | None = None):
        self.device_id = device_id or f"B{uuid.uuid4().hex}"
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self):
        await self._client.aclose()

    def _base_headers(self) -> dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip",
            "Connection": "close",
            "dId": self.device_id,
        }

    def _signed_headers(
        self,
        cred: Credential,
        method: str,
        url: str,
        body_or_query: str,
    ) -> dict[str, str]:
        parsed = urlparse(url)
        source = parsed.query if method.upper() == "GET" else body_or_query
        timestamp = str(int(time.time()) - 2)
        sign_header = {
            "platform": "",
            "timestamp": timestamp,
            "dId": "",
            "vName": "",
        }
        sign_header_json = json.dumps(sign_header, separators=(",", ":"))
        payload = f"{parsed.path}{source}{timestamp}{sign_header_json}"
        sha = hmac.new(
            cred.token.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        sign = hashlib.md5(sha.encode("utf-8")).hexdigest()

        headers = self._base_headers()
        headers.update(
            {
                "cred": cred.cred,
                "sign": sign,
                "platform": sign_header["platform"],
                "timestamp": timestamp,
                "dId": sign_header["dId"],
                "vName": sign_header["vName"],
            }
        )
        return headers

    async def get_grant_code(self, token: str) -> str:
        started = time.time()
        resp = await self._client.post(
            f"{AS_BASE}/user/oauth2/v2/grant",
            headers=self._base_headers(),
            json={"appCode": APP_CODE, "token": token, "type": 0},
        )
        data = resp.json()
        if resp.status_code != 200 or data.get("status") != 0:
            raise SklandAuthError(data.get("msg") or data.get("message") or str(data))
        elapsed = int((time.time() - started) * 1000)
        # 调试定位用：不记录 token，只记录接口阶段和耗时。
        data["_debug_elapsed_ms"] = elapsed
        return str(data["data"]["code"])

    async def send_phone_code(self, phone: str) -> None:
        resp = await self._client.post(
            f"{AS_BASE}/general/v1/send_phone_code",
            headers=self._base_headers(),
            json={"phone": phone, "type": 2},
        )
        data = resp.json()
        if resp.status_code != 200 or data.get("status") != 0:
            raise RuntimeError(data.get("msg") or data.get("message") or str(data))

    async def get_token_by_phone_code(self, phone: str, code: str) -> str:
        resp = await self._client.post(
            f"{AS_BASE}/user/auth/v2/token_by_phone_code",
            headers=self._base_headers(),
            json={"phone": phone, "code": code},
        )
        data = resp.json()
        if resp.status_code != 200 or data.get("status") != 0:
            raise SklandAuthError(data.get("msg") or data.get("message") or str(data))
        return str(data["data"]["token"])

    async def get_credential(self, grant_code: str) -> Credential:
        started = time.time()
        resp = await self._client.post(
            f"{SKLAND_BASE}/web/v1/user/auth/generate_cred_by_code",
            headers=self._base_headers(),
            json={"code": grant_code, "kind": 1},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise SklandAuthError(data.get("message") or str(data))
        data["_debug_elapsed_ms"] = int((time.time() - started) * 1000)
        return Credential(token=data["data"]["token"], cred=data["data"]["cred"])

    async def get_binding_list(self, cred: Credential) -> list[Binding]:
        url = f"{SKLAND_BASE}/api/v1/game/player/binding"
        resp = await self._client.get(
            url,
            headers=self._signed_headers(cred, "GET", url, ""),
        )
        data = resp.json()
        if data.get("code") != 0:
            message = data.get("message") or str(data)
            if "登录" in message or "token" in message.lower() or "cred" in message.lower():
                raise SklandAuthError(message)
            raise RuntimeError(message)

        bindings: list[Binding] = []
        for item in data.get("data", {}).get("list", []):
            app_code = item.get("appCode")
            if app_code not in ("arknights", "endfield"):
                continue
            for binding in item.get("bindingList", []):
                bindings.append(
                    Binding(
                        app_code=app_code or "",
                        game_id=int(binding.get("gameId") or 1),
                        uid=str(binding.get("uid") or ""),
                        nickname=str(binding.get("nickName") or "Unknown"),
                        channel_name=str(binding.get("channelName") or "Unknown"),
                        game_name=str(
                            binding.get("gameName")
                            or ("终末地" if app_code == "endfield" else "明日方舟")
                        ),
                        roles=list(binding.get("roles") or []),
                    )
                )
        return bindings

    async def sign(self, cred: Credential, binding: Binding) -> SignResult:
        if binding.app_code == "endfield":
            results = await self.sign_endfield(cred, binding)
            if len(results) == 1:
                return results[0]
            success = all(item.success or item.already_signed for item in results)
            awards = []
            errors = []
            for item in results:
                awards.extend(item.awards)
                if item.error:
                    errors.append(f"{item.nickname}: {item.error}")
            return SignResult(
                success=success,
                game_name=binding.game_name or "终末地",
                nickname=binding.nickname,
                channel_name=binding.channel_name,
                awards=awards,
                error="; ".join(errors),
            )
        return await self.sign_arknights(cred, binding)

    async def sign_arknights(self, cred: Credential, binding: Binding) -> SignResult:
        url = f"{SKLAND_BASE}/api/v1/game/attendance"
        body = {"gameId": binding.game_id, "uid": binding.uid}
        body_text = json.dumps(body, separators=(",", ":"))
        headers = self._signed_headers(cred, "POST", url, body_text)
        headers["Content-Type"] = "application/json"
        resp = await self._client.post(url, headers=headers, content=body_text)
        data = resp.json()
        if data.get("code") != 0:
            return SignResult(
                success=False,
                game_name=binding.game_name or "明日方舟",
                nickname=binding.nickname,
                channel_name=binding.channel_name,
                error=data.get("message") or str(data),
            )

        awards = []
        for award in data.get("data", {}).get("awards", []):
            resource = award.get("resource") or {}
            name = resource.get("name") or "Unknown"
            count = award.get("count") or 1
            awards.append(f"{name}x{count}")
        return SignResult(
            success=True,
            game_name=binding.game_name or "明日方舟",
            nickname=binding.nickname,
            channel_name=binding.channel_name,
            awards=awards,
        )

    async def sign_endfield(self, cred: Credential, binding: Binding) -> list[SignResult]:
        if not binding.roles:
            return [
                SignResult(
                    success=False,
                    game_name=binding.game_name or "终末地",
                    nickname=binding.nickname,
                    channel_name=binding.channel_name,
                    error="没有终末地角色数据",
                )
            ]

        url = f"{SKLAND_BASE}/web/v1/game/endfield/attendance"
        results: list[SignResult] = []
        for role in binding.roles:
            role_id = str(role.get("roleId") or "")
            server_id = str(role.get("serverId") or "")
            nickname = str(role.get("nickname") or binding.nickname)
            headers = self._signed_headers(cred, "POST", url, "")
            headers["Content-Type"] = "application/json"
            headers["origin"] = "https://game.skland.com"
            headers["referer"] = "https://game.skland.com/"
            headers["sk-game-role"] = f"3_{role_id}_{server_id}"
            resp = await self._client.post(url, headers=headers, content="")
            data = resp.json()
            if data.get("code") != 0:
                results.append(
                    SignResult(
                        success=False,
                        game_name=binding.game_name or "终末地",
                        nickname=nickname,
                        channel_name=binding.channel_name,
                        error=data.get("message") or str(data),
                    )
                )
                continue

            awards = []
            resource_map = data.get("data", {}).get("resourceInfoMap", {})
            for award in data.get("data", {}).get("awardIds", []):
                award_id = str(award.get("id") or "")
                resource = resource_map.get(award_id) or {}
                name = resource.get("name") or "Unknown"
                count = resource.get("count") or award.get("count") or 1
                awards.append(f"{name}x{count}")
            results.append(
                SignResult(
                    success=True,
                    game_name=binding.game_name or "终末地",
                    nickname=nickname,
                    channel_name=binding.channel_name,
                    awards=awards,
                )
            )
        return results

    async def sign_with_token(self, token: str) -> list[SignResult]:
        grant_code = await self.get_grant_code(token)
        cred = await self.get_credential(grant_code)
        bindings = await self.get_binding_list(cred)
        results: list[SignResult] = []
        for binding in bindings:
            if binding.app_code == "endfield":
                results.extend(await self.sign_endfield(cred, binding))
            else:
                results.append(await self.sign_arknights(cred, binding))
        return results
