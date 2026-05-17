from __future__ import annotations

import sys
import types
import unittest
from dataclasses import dataclass, replace
from pathlib import Path

astrbot_module = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
astrbot_api_module = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))


class StubLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(str(message))


stub_logger = StubLogger()
astrbot_api_module.logger = stub_logger

from services.scheduler import ScheduleService


@dataclass(frozen=True)
class FakeScheduleSettings:
    enabled: bool = True
    group_ids: list[str] | None = None
    category: str | None = None


@dataclass(frozen=True)
class FakeSettings:
    schedule: FakeScheduleSettings


@dataclass(frozen=True)
class FakeRecord:
    id: str = "image-id"
    send_count: int = 0


class FakeLibrary:
    def __init__(self) -> None:
        self.record = FakeRecord()
        self.recorded: list[tuple[str, str]] = []

    def select_random(self, *, category, session_id):
        return self.record

    def record_send(self, image_id: str, session_id: str):
        self.recorded.append((image_id, session_id))
        self.record = replace(self.record, send_count=self.record.send_count + 1)
        return self.record


class FakeCommands:
    def send_path_for_record(self, record) -> Path:
        return Path("image.jpg")

    def image_chain(self, image_path: Path):
        return ("image", str(image_path))

    def text_chain(self, text: str):
        return ("text", text)


class FakeContext:
    def __init__(self, *, fail_text: bool = False) -> None:
        self.fail_text = fail_text
        self.sent: list[tuple[str, object]] = []

    async def send_message(self, session: str, chain):
        self.sent.append((session, chain))
        if self.fail_text and chain[0] == "text":
            raise RuntimeError("text failed")
        return True


class FakePlugin:
    def __init__(self, context: FakeContext, library: FakeLibrary) -> None:
        self.context = context
        self.library = library
        self.commands = FakeCommands()
        self.settings = FakeSettings(
            schedule=FakeScheduleSettings(enabled=True, group_ids=["10001"])
        )

    def require_library(self) -> FakeLibrary:
        return self.library

    def image_info_text(self, record) -> str:
        return f"发送次数：{record.send_count}"

    def data_root(self) -> Path:
        return Path(".")


class ScheduleServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_text_failure_does_not_mark_scheduled_image_failed(self) -> None:
        context = FakeContext(fail_text=True)
        library = FakeLibrary()
        service = ScheduleService(FakePlugin(context, library))
        service.group_sessions["10001"] = "session"

        result = await service.send_scheduled_image(target_group_ids=["10001"], force=True)

        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], [])
        self.assertEqual(library.recorded, [("image-id", "session")])
        self.assertEqual(context.sent[0], ("session", ("image", "image.jpg")))


if __name__ == "__main__":
    unittest.main()
