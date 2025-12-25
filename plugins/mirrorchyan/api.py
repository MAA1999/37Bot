"""Mirror API 请求"""

import hashlib
from pathlib import Path
from typing import Optional, Tuple
import httpx

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


async def get_latest_version(
    resource_id: str, resource_type: int, channel: str = "stable", cdk: str = ""
) -> Optional[dict]:
    """
    获取资源最新版本信息

    Args:
        resource_id: 资源ID
        resource_type: 0=通用, 1=跨平台(win-x64)
        channel: stable | beta | alpha
        cdk: CDK密钥

    Returns:
        API返回的data字段，失败返回None
    """
    url = f"{API_BASE}/{resource_id}/latest"
    params = {
        "channel": channel,
        "user_agent": USER_AGENT,
    }
    if resource_type == 1:
        params["os"] = "win"
        params["arch"] = "x64"
    if cdk:
        params["cdk"] = cdk

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
) -> Tuple[bool, str, Optional[dict]]:
    """
    下载资源文件（带hash检测）

    Returns:
        (成功, 错误信息/状态信息, 版本信息)
    """
    url = f"{API_BASE}/{resource_id}/latest"
    params = {
        "channel": channel,
        "user_agent": USER_AGENT,
        "cdk": cdk,
    }
    if resource_type == 1:
        params["os"] = "win"
        params["arch"] = "x64"

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=30)
            result = resp.json()
            code = result.get("code")

            if code != 0:
                err_msg = ERROR_MESSAGES.get(code, result.get("msg", "未知错误"))
                return False, err_msg, None
            if "url" not in result.get("data", {}):
                return False, "无下载链接", None

            data = result["data"]
            expected_sha256 = data.get("sha256", "")

            # 下载前检测：本地文件已存在且hash匹配则跳过
            if expected_sha256 and Path(save_path).exists():
                local_hash = _calc_sha256(save_path)
                if local_hash == expected_sha256:
                    return True, "文件已存在且hash匹配，跳过下载", data

            # 流式下载
            async with client.stream("GET", data["url"], timeout=600, follow_redirects=True) as dl_resp:
                if dl_resp.status_code != 200:
                    return False, f"下载失败: {dl_resp.status_code}", None
                with open(save_path, "wb") as f:
                    async for chunk in dl_resp.aiter_bytes(chunk_size=8192):
                        f.write(chunk)

            # 下载后校验
            if expected_sha256:
                actual_hash = _calc_sha256(save_path)
                if actual_hash != expected_sha256:
                    Path(save_path).unlink(missing_ok=True)
                    return False, f"hash校验失败: 期望{expected_sha256[:16]}... 实际{actual_hash[:16]}...", None

            return True, "", data
    except Exception as e:
        return False, str(e), None
