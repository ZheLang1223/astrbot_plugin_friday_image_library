from __future__ import annotations

import asyncio
import base64
import os
import re
import tempfile
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+")


class ImageExtractionError(Exception):
    """Raised when an incoming image cannot be resolved or validated."""


@dataclass(frozen=True)
class ExtractedImage:
    path: Path
    source_name: str
    extension: str
    size: int


@dataclass
class ExtractionResult:
    images: list[ExtractedImage] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def normalize_extensions(extensions: Iterable[str] | str | None) -> set[str]:
    if extensions is None:
        return {"jpg", "jpeg", "png", "gif", "webp"}
    if isinstance(extensions, str):
        raw_items = re.split(r"[\s,，;；]+", extensions)
    else:
        raw_items = [str(item) for item in extensions]
    normalized = {item.strip().lower().lstrip(".") for item in raw_items if item.strip()}
    if "jpg" in normalized:
        normalized.add("jpeg")
    if "jpeg" in normalized:
        normalized.add("jpg")
    return normalized or {"jpg", "jpeg", "png", "gif", "webp"}


def safe_filename(value: str | None, fallback: str = "image", max_length: int = 48) -> str:
    name = Path(value or "").stem or fallback
    name = _SAFE_NAME_RE.sub("_", name).strip("._- ")
    if not name:
        name = fallback
    return name[:max_length]


def detect_image_extension(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    try:
        with path.open("rb") as file_obj:
            header = file_obj.read(16)
    except OSError as exc:
        raise ImageExtractionError(f"无法读取图片文件：{path}") from exc

    if header.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    if suffix in {"jpg", "jpeg", "png", "gif", "webp"}:
        return suffix
    raise ImageExtractionError("无法识别图片格式，仅支持 jpg/jpeg/png/gif/webp。")


def validate_image_file(
    path: Path,
    allowed_extensions: Iterable[str] | str | None,
    max_size_bytes: int,
) -> ExtractedImage:
    path = Path(path)
    if not path.exists() or not path.is_file():
        raise ImageExtractionError("图片文件不存在或不是普通文件。")
    size = path.stat().st_size
    if max_size_bytes > 0 and size > max_size_bytes:
        limit_mb = max_size_bytes / 1024 / 1024
        raise ImageExtractionError(f"图片超过大小上限：{limit_mb:.0f}MB。")
    extension = detect_image_extension(path)
    allowed = normalize_extensions(allowed_extensions)
    if extension not in allowed and not (extension == "jpg" and "jpeg" in allowed):
        allowed_text = ", ".join(sorted(allowed))
        raise ImageExtractionError(f"不支持的图片格式：{extension}。允许格式：{allowed_text}。")
    return ExtractedImage(
        path=path,
        source_name=path.name,
        extension="jpg" if extension == "jpeg" else extension,
        size=size,
    )


async def extract_images_from_event(
    event: Any,
    *,
    allowed_extensions: Iterable[str] | str | None,
    max_size_bytes: int,
) -> ExtractionResult:
    result = ExtractionResult()
    components = _event_message_components(event)
    image_components = list(_iter_image_components(components))
    temp_dir = Path(tempfile.gettempdir()) / "astrbot_plugin_friday_image_library"
    temp_dir.mkdir(parents=True, exist_ok=True)

    for component in image_components:
        try:
            path = await _component_to_file_path(component, temp_dir)
            extracted = validate_image_file(path, allowed_extensions, max_size_bytes)
            result.images.append(extracted)
        except ImageExtractionError as exc:
            result.errors.append(str(exc))
        except Exception as exc:  # pragma: no cover - defensive guard for adapter quirks
            result.errors.append(f"图片解析失败：{exc}")
    return result


def _event_message_components(event: Any) -> list[Any]:
    get_messages = getattr(event, "get_messages", None)
    if callable(get_messages):
        messages = get_messages()
        return list(messages or [])
    message_obj = getattr(event, "message_obj", None)
    messages = getattr(message_obj, "message", None)
    return list(messages or [])


def _iter_image_components(components: Iterable[Any]) -> Iterable[Any]:
    for component in components:
        if _is_image_component(component):
            yield component
        for nested in _nested_components(component):
            yield from _iter_image_components(nested)


def _is_image_component(component: Any) -> bool:
    if isinstance(component, dict):
        return str(component.get("type", "")).lower() == "image"
    type_value = getattr(component, "type", "")
    type_text = str(getattr(type_value, "value", type_value)).lower()
    return component.__class__.__name__.lower() == "image" or type_text == "image"


def _nested_components(component: Any) -> list[list[Any]]:
    nested: list[list[Any]] = []
    if isinstance(component, dict):
        data = component.get("data") or {}
        for key in ("chain", "message", "content"):
            value = data.get(key)
            if isinstance(value, list):
                nested.append(value)
        return nested

    for key in ("chain", "message", "content"):
        value = getattr(component, key, None)
        if isinstance(value, list):
            nested.append(value)
    return nested


async def _component_to_file_path(component: Any, temp_dir: Path) -> Path:
    convert = getattr(component, "convert_to_file_path", None)
    if callable(convert):
        converted = await convert()
        path = Path(converted)
        if path.exists():
            return path

    payload = _component_payload(component)
    for value in payload:
        if not value:
            continue
        value = str(value)
        if value.startswith("file://"):
            path = _file_uri_to_path(value)
            if path.exists():
                return path
        if value.startswith("base64://"):
            return _write_base64_to_temp(value.removeprefix("base64://"), temp_dir)
        if value.startswith("http://") or value.startswith("https://"):
            return await asyncio.to_thread(_download_to_temp, value, temp_dir)
        path = Path(value)
        if path.exists():
            return path

    raise ImageExtractionError("未能从消息中取得可读取的图片文件。")


def _component_payload(component: Any) -> list[Any]:
    if isinstance(component, dict):
        data = component.get("data") or {}
        return [data.get("path"), data.get("file"), data.get("url")]
    return [
        getattr(component, "path", None),
        getattr(component, "file", None),
        getattr(component, "url", None),
    ]


def _file_uri_to_path(uri: str) -> Path:
    parsed = urllib.parse.urlparse(uri)
    path = urllib.parse.unquote(parsed.path)
    if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return Path(path)


def _write_base64_to_temp(payload: str, temp_dir: Path) -> Path:
    data = base64.b64decode(payload)
    path = temp_dir / f"incoming_{uuid.uuid4().hex}.img"
    path.write_bytes(data)
    return path


def _download_to_temp(url: str, temp_dir: Path) -> Path:
    request = urllib.request.Request(url, headers={"User-Agent": "AstrBot-Friday-Image-Library/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read()
        suffix = _suffix_from_url_or_content_type(url, response.headers.get("Content-Type", ""))
    path = temp_dir / f"incoming_{uuid.uuid4().hex}{suffix}"
    path.write_bytes(data)
    return path


def _suffix_from_url_or_content_type(url: str, content_type: str) -> str:
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return suffix
    content_type = content_type.lower().split(";")[0].strip()
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(content_type, ".img")
