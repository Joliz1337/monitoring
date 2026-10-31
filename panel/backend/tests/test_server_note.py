"""Подпись сервера на дашборде: пробелы обрезаются, пустая строка снимает
подпись, слишком длинная отклоняется."""

import os
import sys
import unittest

from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models import SERVER_NOTE_MAX_LENGTH  # noqa: E402
from app.routers.servers import ServerUpdate  # noqa: E402


class ServerNoteTests(unittest.TestCase):
    def test_trims_spaces(self):
        self.assertEqual(ServerUpdate(note="  основной  ").note, "основной")

    def test_blank_clears_note(self):
        update = ServerUpdate(note="   ")

        self.assertIsNone(update.note)
        self.assertEqual(update.model_dump(exclude_unset=True), {"note": None})

    def test_rejects_too_long(self):
        with self.assertRaises(ValidationError):
            ServerUpdate(note="x" * (SERVER_NOTE_MAX_LENGTH + 1))

    def test_untouched_when_not_sent(self):
        self.assertNotIn("note", ServerUpdate(name="node-1").model_dump(exclude_unset=True))


if __name__ == "__main__":
    unittest.main()
