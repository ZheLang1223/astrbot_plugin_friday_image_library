from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .image_io import (
    ExtractedImage,
    ExtractionResult,
    ImageExtractionError,
    extract_images_from_event,
    validate_image_file,
)
from .image_library import (
    ImageLibrary,
    ImageLibraryError,
    ImageRecord,
    UnsupportedImageType,
)
from .settings import UploadSettings


INBOX_DISPLAY_NAME = "待整理"


@dataclass(frozen=True)
class UploadRequest:
    category: str | None
    uploader_id: str
    source_session: str


@dataclass
class UploadSummary:
    saved_count: int = 0
    duplicate_count: int = 0
    failed: list[str] = field(default_factory=list)
    records: list[ImageRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "saved_count": self.saved_count,
            "duplicate_count": self.duplicate_count,
            "failed": self.failed,
            "record_ids": [record.id for record in self.records],
        }

    @property
    def status(self) -> str:
        if self.saved_count > 0:
            return "saved"
        if self.duplicate_count > 0:
            return "duplicate"
        return "failed"


class UploadPipeline:
    def __init__(self, library: ImageLibrary, settings: UploadSettings) -> None:
        self.library = library
        self.settings = settings
        self._ensure_inbox_category()

    async def upload_event(self, event, request: UploadRequest) -> UploadSummary:
        extraction = await extract_images_from_event(
            event,
            allowed_extensions=self.settings.allowed_extensions,
            max_size_bytes=self.settings.max_size_bytes,
        )
        return self.upload_extracted(extraction, request)

    def upload_file(
        self,
        path: Path,
        filename: str,
        request: UploadRequest,
    ) -> UploadSummary:
        try:
            image = validate_image_file(
                path,
                allowed_extensions=self.settings.allowed_extensions,
                max_size_bytes=self.settings.max_size_bytes,
            )
        except ImageExtractionError as exc:
            return UploadSummary(failed=[str(exc)])
        image = ExtractedImage(
            path=image.path,
            source_name=filename or image.source_name,
            extension=image.extension,
            size=image.size,
        )
        return self.upload_images([image], request)

    def upload_extracted(
        self,
        extraction: ExtractionResult,
        request: UploadRequest,
    ) -> UploadSummary:
        summary = self.upload_images(extraction.images, request)
        summary.failed.extend(extraction.errors)
        return summary

    def upload_images(
        self,
        images: Iterable[ExtractedImage],
        request: UploadRequest,
    ) -> UploadSummary:
        category = self._category_or_inbox(request.category)
        summary = UploadSummary()
        for image in images:
            try:
                result = self.library.add_image(
                    category=category,
                    source_path=image.path,
                    original_name=image.source_name,
                    detected_extension=image.extension,
                    uploader_id=request.uploader_id,
                    source_session=request.source_session,
                )
            except (ImageLibraryError, UnsupportedImageType) as exc:
                summary.failed.append(str(exc))
                continue
            summary.records.append(result.record)
            if result.status == "duplicate":
                summary.duplicate_count += 1
            else:
                summary.saved_count += 1
        return summary

    def _category_or_inbox(self, category: str | None) -> str:
        category = (category or "").strip()
        if category:
            return category
        return self.settings.inbox_category

    def _ensure_inbox_category(self) -> None:
        inbox = (self.settings.inbox_category or "inbox").strip() or "inbox"
        if inbox == "inbox":
            self.library.create_category("inbox", INBOX_DISPLAY_NAME)
            return
        self.library.create_category_from_input(inbox)
