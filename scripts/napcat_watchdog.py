#!/usr/bin/env python3
"""NapCat 在线看门狗。

每 3 分钟由 systemd timer 调用一次：
- 通过 OneBot11 WebSocket 调用 get_status 判断 QQ 是否在线
- 离线时重启 napcat 容器（一次「故障」只重启一次）
- 重启后仍离线（通常是账号被踢、需要扫码）就不再重启，留下人工标记
"""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/home/37Bot")
DATA = ROOT / "data"
LOG = ROOT / "logs/napcat-watchdog.log"
MANUAL_FLAG = DATA / ".napcat_offline_manual"
FAIL_COUNT = DATA / ".napcat_wd_failcount"
CONFIG = ROOT / "napcat" / "config" / "onebot11_1057521489.json"
WS_URL = "ws://127.0.0.1:3001"
CONTAINER = "napcat"
RESTART_WAIT = 75
RESTART_FAIL_LIMIT = 2


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def ws_url() -> str:
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        for server in cfg.get("network", {}).get("websocketServers", []):
            token = server.get("token") or ""
            if token:
                return f"{WS_URL}?access_token={token}"
    except (OSError, ValueError):
        pass
    return WS_URL


async def query_online(timeout: float = 10.0) -> bool | None:
    """返回 True/False 表示在线状态，None 表示拿不到可信答案。

    连接后 NapCat 会先推一条 lifecycle 元事件，必须等到 echo 匹配的 API 响应。
    """
    import websockets

    deadline = time.monotonic() + timeout
    async with websockets.connect(ws_url(), open_timeout=timeout, close_timeout=2) as ws:
        await ws.send(json.dumps({"action": "get_status", "echo": "watchdog"}))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log("等待 get_status 响应超时")
                return None
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            if isinstance(raw, (bytes, bytearray)):
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, dict) or message.get("echo") != "watchdog":
                continue
            if message.get("retcode") not in (0, None):
                log(f"get_status 返回异常: {message.get('retcode')} {message.get('message', '')}")
                return None
            data = message.get("data") or {}
            return bool(data.get("online"))


def check_online() -> bool | None:
    """True 在线 / False 离线 / None 连不上 NapCat。"""
    try:
        return asyncio.run(query_online())
    except Exception as exc:  # noqa: BLE001 - 需要把任何连接异常都当作不可判定
        log(f"状态查询失败: {type(exc).__name__}: {exc}")
        return None


def read_fail_count() -> int:
    try:
        return int(FAIL_COUNT.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def write_fail_count(value: int) -> None:
    try:
        if value <= 0:
            FAIL_COUNT.unlink(missing_ok=True)
        else:
            FAIL_COUNT.write_text(str(value), encoding="utf-8")
    except OSError:
        pass


def restart_container() -> bool:
    log(f"重启容器 {CONTAINER}")
    try:
        result = subprocess.run(
            ["docker", "restart", CONTAINER],
            capture_output=True, text=True, timeout=240, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"重启失败: {type(exc).__name__}: {exc}")
        return False
    if result.returncode != 0:
        log(f"重启失败: exit={result.returncode} stderr={result.stderr.strip()[:200]}")
        return False
    log(f"等待 {RESTART_WAIT}s 让 NapCat 登录")
    time.sleep(RESTART_WAIT)
    return True


def main() -> int:
    state = check_online()

    if state is True:
        write_fail_count(0)
        if MANUAL_FLAG.exists():
            MANUAL_FLAG.unlink(missing_ok=True)
            log("已恢复在线，清除人工标记")
        return 0

    if state is None:
        fails = read_fail_count() + 1
        write_fail_count(fails)
        log(f"无法连接 NapCat（连续 {fails} 次）")
        if fails < RESTART_FAIL_LIMIT:
            return 1
    else:
        fails = read_fail_count()
        write_fail_count(0)
        log("QQ 处于离线状态")

    if MANUAL_FLAG.exists():
        log("已标记需人工扫码，跳过自动重启")
        return 1

    if not restart_container():
        return 1

    state = check_online()
    if state is True:
        write_fail_count(0)
        log("重启后已恢复在线")
        return 0

    try:
        MANUAL_FLAG.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    except OSError:
        pass
    log("重启后仍未在线，需要人工扫码登录；已写入标记，停止自动重启")
    return 1


if __name__ == "__main__":
    sys.exit(main())
