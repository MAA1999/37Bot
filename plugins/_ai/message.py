"""Message cleaning helpers shared by LLM-backed plugins."""

import base64
import mimetypes
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from ncatbot.utils import get_log

from . import get_vision_llm, is_vision_llm_configured

logger = get_log("AiMessage")

MAX_IMAGES_PER_MESSAGE = 3
MAX_IMAGE_BYTES = 6 * 1024 * 1024


def _seg_type(seg) -> str:
    return str(getattr(seg, "msg_seg_type", "") or getattr(seg, "type", "")).lower()


def _seg_text(seg) -> str:
    return str(getattr(seg, "text", "") or "")


def _seg_summary(seg) -> str:
    try:
        summary = seg.get_summary()
    except Exception:
        summary = ""
    if summary and summary != "该消息不支持预览":
        return str(summary)
    return ""


def is_image_segment(seg) -> bool:
    return _seg_type(seg) == "image"


def has_image(message) -> bool:
    return any(is_image_segment(seg) for seg in message)


def clean_plain_text(message) -> str:
    """Build text without LLM image analysis."""
    parts: list[str] = []
    for seg in message:
        seg_type = _seg_type(seg)
        if seg_type in ("text", "plain"):
            parts.append(_seg_text(seg))
        elif seg_type == "at":
            qq = getattr(seg, "qq", "") or getattr(seg, "user_id", "")
            parts.append(f"@{qq}" if str(qq) != "all" else "@全体成员")
        elif seg_type == "reply":
            continue
        elif seg_type == "image":
            parts.append("[图片]")
        else:
            summary = _seg_summary(seg)
            if summary:
                parts.append(summary)
    return "".join(parts).strip()


def extract_text_only(message) -> str:
    parts = []
    for seg in message:
        if _seg_type(seg) in ("text", "plain"):
            text = _seg_text(seg)
            if text:
                parts.append(text)
    text = " ".join(parts).strip()
    return re.sub(r"^/\S+\s*", "", text).strip()


def _is_http_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _is_data_url(value: str) -> bool:
    return value.startswith("data:image/")


def _image_source(seg) -> str:
    for attr in ("url", "file"):
        value = str(getattr(seg, attr, "") or "")
        if value:
            return value
    try:
        data = seg.to_dict().get("data", {})
    except Exception:
        data = {}
    return str(data.get("url") or data.get("file") or "")


def _mime_for_path(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "image/jpeg"


def local_image_to_data_url(path: Path) -> str:
    size = path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        return "[图片: 过大未分析]"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{_mime_for_path(path)};base64,{data}"


async def image_segment_to_url(seg, tmp_dir: Path | None = None) -> str:
    source = _image_source(seg)
    if _is_http_url(source) or _is_data_url(source):
        return source

    if source:
        parsed = urlparse(source)
        path_text = parsed.path if parsed.scheme == "file" else source
        path = Path(path_text)
        if path.exists() and path.is_file():
            return local_image_to_data_url(path)

    if tmp_dir is None:
        tmp_dir = Path(tempfile.gettempdir()) / "37bot_vision"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    name = str(getattr(seg, "file_name", "") or getattr(seg, "file", "") or "image")
    name = Path(name).name or "image"

    for method_name in ("download_to", "download"):
        method = getattr(seg, method_name, None)
        if not method:
            continue
        before = set(tmp_dir.iterdir())
        try:
            result = await method(tmp_dir, name=name)
        except TypeError:
            try:
                result = await method(str(tmp_dir), name=name)
            except Exception as exc:
                logger.debug(f"图片下载失败 ({method_name}): {exc}")
                continue
        except Exception as exc:
            logger.debug(f"图片下载失败 ({method_name}): {exc}")
            continue

        candidates = []
        if result:
            candidates.append(Path(result))
        after = set(tmp_dir.iterdir())
        candidates.extend(sorted(after - before, key=lambda p: p.stat().st_mtime, reverse=True))
        candidates.extend(sorted(tmp_dir.glob(name + "*"), key=lambda p: p.stat().st_mtime, reverse=True))
        for path in candidates:
            if path.exists() and path.is_file():
                return local_image_to_data_url(path)

    return "[图片]"


async def analyze_image_segment(seg, hint: str = "", tmp_dir: Path | None = None) -> str:
    if not is_vision_llm_configured():
        return "[图片]"
    try:
        image_url = await image_segment_to_url(seg, tmp_dir=tmp_dir)
    except Exception as exc:
        logger.warning(f"图片准备失败: {exc}")
        return "[图片]"
    if image_url.startswith("[图片"):
        return image_url
    try:
        result = await get_vision_llm().analyze_image(image_url, hint=hint)
    except Exception as exc:
        logger.warning(f"图片分析异常: {exc}")
        return "[图片]"
    if not result:
        return "[图片]"
    result = re.sub(r"\s+", " ", result).strip()
    return f"[图片分析: {result[:160]}]"


async def clean_message_for_llm(
    message,
    analyze_images: bool = False,
    image_hint: str = "",
    tmp_dir: Path | None = None,
    max_images: int = MAX_IMAGES_PER_MESSAGE,
) -> str:
    parts: list[str] = []
    analyzed = 0
    for seg in message:
        seg_type = _seg_type(seg)
        if seg_type in ("text", "plain"):
            parts.append(_seg_text(seg))
        elif seg_type == "at":
            qq = getattr(seg, "qq", "") or getattr(seg, "user_id", "")
            parts.append(f"@{qq}" if str(qq) != "all" else "@全体成员")
        elif seg_type == "reply":
            continue
        elif seg_type == "image":
            if analyze_images and analyzed < max_images:
                parts.append(await analyze_image_segment(seg, hint=image_hint, tmp_dir=tmp_dir))
                analyzed += 1
            else:
                parts.append("[图片]")
        else:
            summary = _seg_summary(seg)
            if summary:
                parts.append(summary)
    return "".join(parts).strip()
