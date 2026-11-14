import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routers import backup as bk  # noqa: E402

RAW_DUMP = b"PGDMP\x00\x01binary-dump-bytes"
ENV_TEXT = "PANEL_ENC_KEY=a2V5\nJWT_SECRET=jjj\nPOSTGRES_PASSWORD=frombackup\n# comment\n"


def unframe(backup: bytes) -> tuple[bytes, dict]:
    """Чистый дамп и .env бэкапа — по смещению, с которого pg_restore читает файл."""
    dump_offset, env = bk._read_frame(io.BytesIO(backup))
    return backup[dump_offset:], env


class BackupFramingTests(unittest.TestCase):
    def test_v2_roundtrip_full_env(self):
        framed = bk._frame_header(ENV_TEXT) + RAW_DUMP
        self.assertTrue(framed.startswith(bk._BACKUP_MAGIC_V2))
        dump, env = unframe(framed)
        self.assertEqual(dump, RAW_DUMP)
        self.assertEqual(env["PANEL_ENC_KEY"], "a2V5")
        self.assertEqual(env["JWT_SECRET"], "jjj")

    def test_v2_layout_on_disk_is_unchanged(self):
        # Старые бэкапы должны восстанавливаться, а новые — читаться прежними версиями панели
        env = "PANEL_ENC_KEY=ключ\n"
        self.assertEqual(bk._frame_header(env), b"MONBKP2\n" + b"23\n" + env.encode("utf-8"))

    def test_no_env_is_raw(self):
        self.assertEqual(bk._frame_header(None), b"")

    def test_legacy_raw_dump_passthrough(self):
        dump, env = unframe(RAW_DUMP)
        self.assertEqual(dump, RAW_DUMP)
        self.assertEqual(env, {})

    def test_legacy_v1_key_only(self):
        framed = bk._BACKUP_MAGIC + b"a2V5\n" + RAW_DUMP
        dump, env = unframe(framed)
        self.assertEqual(dump, RAW_DUMP)
        self.assertEqual(env, {"PANEL_ENC_KEY": "a2V5"})

    def test_legacy_v1_without_a_key(self):
        dump, env = unframe(bk._BACKUP_MAGIC + b"\n" + RAW_DUMP)
        self.assertEqual((dump, env), (RAW_DUMP, {}))

    def test_dump_with_newlines_preserved(self):
        payload = b"PGDMP\nline1\nline2\x00\xff"
        dump, _ = unframe(bk._frame_header(ENV_TEXT) + payload)
        self.assertEqual(dump, payload)

    def test_frame_is_read_from_the_start_wherever_the_file_was_left(self):
        backup = io.BytesIO(bk._frame_header(ENV_TEXT) + RAW_DUMP)
        backup.seek(0, os.SEEK_END)
        dump_offset, env = bk._read_frame(backup)
        self.assertEqual(backup.getvalue()[dump_offset:], RAW_DUMP)
        self.assertEqual(env["JWT_SECRET"], "jjj")

    def test_parse_env(self):
        parsed = bk.parse_env(ENV_TEXT)
        self.assertEqual(parsed["JWT_SECRET"], "jjj")
        self.assertNotIn("# comment", parsed)


class ApplyRestoredEnvTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False)
        self._tmp.write("POSTGRES_PASSWORD=current\nDOMAIN=live.example\nPANEL_UID=old\n")
        self._tmp.close()
        self._orig = bk.PANEL_ENV_PATH
        bk.PANEL_ENV_PATH = Path(self._tmp.name)

    def tearDown(self):
        bk.PANEL_ENV_PATH = self._orig
        os.unlink(self._tmp.name)
        os.environ.pop("PANEL_ENC_KEY", None)

    def test_preserves_infra_applies_rest(self):
        bk.apply_restored_env({
            "POSTGRES_PASSWORD": "frombackup",  # инфра — не трогаем
            "DOMAIN": "old.example",             # инфра — не трогаем
            "PANEL_UID": "restored",             # применяем
            "JWT_SECRET": "newjwt",              # применяем (добавится)
            "PANEL_ENC_KEY": "theKey",           # применяем + в окружение
        })
        result = bk.parse_env(Path(self._tmp.name).read_text())
        self.assertEqual(result["POSTGRES_PASSWORD"], "current")
        self.assertEqual(result["DOMAIN"], "live.example")
        self.assertEqual(result["PANEL_UID"], "restored")
        self.assertEqual(result["JWT_SECRET"], "newjwt")
        self.assertEqual(result["PANEL_ENC_KEY"], "theKey")
        self.assertEqual(os.environ.get("PANEL_ENC_KEY"), "theKey")


if __name__ == "__main__":
    unittest.main()
