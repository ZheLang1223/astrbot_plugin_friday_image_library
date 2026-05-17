from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.upload_pipeline import UploadSummary
from services.web_api import WebApiService


class FakePipeline:
    def upload_file(self, path, filename, request):
        return UploadSummary(failed=["不支持的图片格式。"])


class FakePlugin:
    def upload_pipeline(self) -> FakePipeline:
        return FakePipeline()

    def image_dict(self, record):
        return {}


class WebApiServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_upload_failure_without_records_returns_bad_request(self) -> None:
        service = WebApiService(FakePlugin(), "plugin")
        captured: list[tuple[dict[str, object], int]] = []

        def capture_json(payload: dict[str, object], status: int = 200):
            captured.append((payload, status))
            return {"payload": payload, "status": status}

        service.json = capture_json
        with tempfile.TemporaryDirectory() as tmp:
            temp_path = Path(tmp) / "bad.txt"
            temp_path.write_text("not image", encoding="utf-8")

            async def uploaded_file():
                return temp_path, "bad.txt"

            service.uploaded_file = uploaded_file
            response = await service.api_upload()

        self.assertEqual(response["status"], 400)
        self.assertFalse(response["payload"]["ok"])
        self.assertEqual(captured[0][1], 400)


if __name__ == "__main__":
    unittest.main()
