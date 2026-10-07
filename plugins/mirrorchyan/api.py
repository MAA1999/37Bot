"""Mirror API 请求"""

import hashlib
import urllib.parse
from pathlib import Path
from typing import Optional, Tuple
import httpx

from .config import resolve_type

API_BASE = "https://mirrorchyan.com/api/resources"
USER_AGENT = "37Bot"

# 错误码
ERROR_MESSAGES = {
    1001: "参数不正确",
    7001: "CDK已过期",
    7002: "CDK错误",
    7003: "CDK今日下载次数已达上限",
    7004: "CDK类型和资源不匹配",
    7005: "CDK已被封禁",
    8001: "资源不存在",
    8002: "错误的系统参数",
    8003: "错误的架构参数",
    8004: "错误的更新通道参数",
}


def _calc_sha256(file_path: str) -> str:
    """计算文件SHA256"""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _build_params(
    resource_id: str, resource_type: int, channel: str, cdk: str = ""
) -> dict:
    """拼装请求参数，os/arch 由资源类型决定（见 config.RESOURCE_TYPES）"""
    os_name, arch, _, _ = resolve_type(resource_type)
    params = {
        "channel": channel,
        "user_agent": USER_AGENT,
    }
    if os_name:
        params["os"] = os_name
    if arch:
        params["arch"] = arch
    if cdk:
        params["cdk"] = cdk
    return params


def _ext_from_url(url: str) -> str:
    """从下载 URL 推导文件后缀，取不到返回空串

    MirrorChyan 的下载链接是 302 到
    https://download2.mirrorchyan.com/<rid>/<ver>/<os>-<arch>/<真实文件名>
    真实文件名带的才是权威后缀：linux 是 resource.tar.gz、windows 是 resource.zip、
    macOS 是 <rid>-<ver>-macos-<arch>.dmg，按平台写死会错。
    """
    name = urllib.parse.urlparse(url or "").path.rsplit("/", 1)[-1]
    lower = name.lower()
    for ext in (".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst", ".tgz", ".apk", ".dmg", ".zip", ".7z", ".exe"):
        if lower.endswith(ext):
            return name[len(name) - len(ext):]
    return ""


async def probe_ext(url: str) -> str:
    """按 302 跳转的目标文件名推导后缀；拿不到就退回 API 给的 URL 本身"""
    ext = _ext_from_url(url)
    if ext:
        return ext
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=30, follow_redirects=False)
            if resp.status_code in (301, 302, 303, 307, 308):
                return _ext_from_url(resp.headers.get("location", ""))
    except Exception:
        pass
    return ""


def apply_ext(path: str, ext: str) -> str:
    """把探测到的真实后缀补到保存路径末尾；已有等价后缀（不分大小写）则原样返回

    调用方拿到的后缀来自 download_resource 的权威探测，可能与调用方自己
    预估的不一致（大小写不同或没探测到），统一在这里对齐。
    """
    if ext and not path.lower().endswith(ext.lower()):
        return path + ext
    return path


async def get_latest_version(
    resource_id: str, resource_type: int, channel: str = "stable", cdk: str = ""
) -> Optional[dict]:
    """
    获取资源最新版本信息

    Args:
        resource_id: 资源ID
        resource_type: 见 config.RESOURCE_TYPES
        channel: stable | beta | alpha
        cdk: CDK密钥

    Returns:
        API返回的data字段，失败返回None
    """
    url = f"{API_BASE}/{resource_id}/latest"
    params = _build_params(resource_id, resource_type, channel, cdk)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=30)
            data = resp.json()
            if data.get("code") == 0:
                return data.get("data")
    except Exception:
        pass
    return None


async def download_resource(
    resource_id: str, resource_type: int, channel: str, cdk: str, save_path: str
) -> Tuple[bool, str, Optional[dict], str]:
    """
    下载资源文件（带hash检测）

    Returns:
        (成功, 错误信息/状态信息, 版本信息, 真实文件后缀)
    """
    url = f"{API_BASE}/{resource_id}/latest"
    params = _build_params(resource_id, resource_type, channel, cdk)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=30)
            result = resp.json()
            code = result.get("code")

            if code != 0:
                err_msg = ERROR_MESSAGES.get(code, result.get("msg", "未知错误"))
                return False, err_msg, None, ""
            if "url" not in result.get("data", {}):
                return False, "无下载链接", None, ""

            data = result["data"]
            expected_sha256 = data.get("sha256", "")
            ext = await probe_ext(data["url"])
            # 调用方拼保存名时不一定拿得到下载 URL（无 CDK 的 /latest 不返回 url），
            # 探测可能失败；这里用带 CDK 的 URL 再探测一次，并补全后缀
            save_path = apply_ext(save_path, ext)

            # 下载前检测：本地文件已存在且hash匹配则跳过
            if expected_sha256 and Path(save_path).exists():
                local_hash = _calc_sha256(save_path)
                if local_hash == expected_sha256:
                    return True, "文件已存在且hash匹配，跳过下载", data, ext

            # 流式下载
            async with client.stream("GET", data["url"], timeout=600, follow_redirects=True) as dl_resp:
                if dl_resp.status_code != 200:
                    return False, f"下载失败: {dl_resp.status_code}", None, ext
                with open(save_path, "wb") as f:
                    async for chunk in dl_resp.aiter_bytes(chunk_size=8192):
                        f.write(chunk)

            # 下载后校验
            if expected_sha256:
                actual_hash = _calc_sha256(save_path)
                if actual_hash != expected_sha256:
                    Path(save_path).unlink(missing_ok=True)
                    return False, f"hash校验失败: 期望{expected_sha256[:16]}... 实际{actual_hash[:16]}...", None, ext

            return True, "", data, ext
    except Exception as e:
        return False, str(e), None, ""
