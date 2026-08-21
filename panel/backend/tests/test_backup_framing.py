import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routers import backup as bk  # noqa: E402

RAW_DUMP = b"PGDMP\x00\x01binary-dump-bytes"
ENV_TEXT = "PANEL_ENC_KEY=a2V5\nJWT_SECRET=jjj\nPOSTGRES_PASSWORD=frombackup\n# comment\n"


class BackupFramingTests(unittest.TestCase):
    def test_v2_roundtrip_full_env(self):
        framed = bk._frame_backup(RAW_DUMP, ENV_TEXT)
        self.assertTrue(framed.startswith(bk._BACKUP_MAGIC_V2))
        dump, env = bk._unframe_backup(framed)
        self.assertEqual(dump, RAW_DUMP)
        self.assertEqual(env["PANEL_ENC_KEY"], "a2V5")
        self.assertEqual(env["JWT_SECRET"], "jjj")

    def test_no_env_is_raw(self):
        self.assertEqual(bk._frame_backup(RAW_DUMP, None), RAW_DUMP)

    def test_legacy_raw_dump_passthrough(self):
        dump, env = bk._unframe_backup(RAW_DUMP)
        self.assertEqual(dump, RAW_DUMP)
        self.assertEqual(env, {})

    def test_legacy_v1_key_only(self):
        framed = bk._BACKUP_MAGIC + b"a2V5\n" + RAW_DUMP
        dump, env = bk._unframe_backup(framed)
        self.assertEqual(dump, RAW_DUMP)
        self.assertEqual(env, {"PANEL_ENC_KEY": "a2V5"})

    def test_dump_with_newlines_preserved(self):
        payload = b"PGDMP\nline1\nline2\x00\xff"
        dump, _ = bk._unframe_backup(bk._frame_backup(payload, ENV_TEXT))
        self.assertEqual(dump, payload)

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
