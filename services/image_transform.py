from __future__ import annotations

from pathlib import Path


class ImageTransformError(Exception):
    """Raised when a send-time image transform cannot be prepared."""


def transformed_send_path(record, transform_root: Path | str) -> Path:
    if record.send_transform == "none" or record.safety_status == "normal":
        return record.path
    if record.send_transform == "rotate_180":
        return rotate_180_for_send(record, transform_root)
    raise ImageTransformError(f"不支持的发送变换：{record.send_transform}")


def rotate_180_for_send(record, transform_root: Path | str) -> Path:
    if record.extension.lower() == "gif":
        raise ImageTransformError("GIF 图片暂不支持旋转 180 度后发送。")
    try:
        from PIL import Image
    except ImportError as exc:
        raise ImageTransformError("敏感图旋转发送需要 Pillow：请安装 pillow 后重载插件。") from exc

    source = record.path
    if not source.exists() or not source.is_file():
        raise ImageTransformError("原图文件不存在，无法生成旋转副本。")

    transform_root = Path(transform_root)
    transform_root.mkdir(parents=True, exist_ok=True)
    suffix = ".jpg" if record.extension.lower() in {"jpg", "jpeg"} else f".{record.extension.lower()}"
    target = (
        transform_root
        / f"{record.short_id}-rotate_180-{source.stat().st_mtime_ns}{suffix}"
    )
    if target.exists():
        return target

    with Image.open(source) as image:
        rotated = image.transpose(Image.Transpose.ROTATE_180)
        save_kwargs = _save_kwargs(record.extension)
        if record.extension.lower() in {"jpg", "jpeg"} and rotated.mode not in {"RGB", "L"}:
            rotated = rotated.convert("RGB")
        rotated.save(target, **save_kwargs)
    return target


def _save_kwargs(extension: str) -> dict[str, object]:
    extension = extension.lower()
    if extension in {"jpg", "jpeg"}:
        return {"format": "JPEG", "quality": 95, "subsampling": 0}
    if extension == "png":
        return {"format": "PNG"}
    if extension == "webp":
        return {"format": "WEBP", "lossless": True, "quality": 100}
    return {}
