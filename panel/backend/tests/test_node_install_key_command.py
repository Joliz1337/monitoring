"""Команда установки из карточки «Ключи установки» следует каналу обновлений.

install.sh не узнаёт ветку по URL, с которого его скачали, — без MON_BRANCH
он клонирует main и пишет MON_IMAGE_TAG=latest даже из dev-установщика.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import update_channel  # noqa: E402
from app.services.node_install_keys import build_install_command  # noqa: E402

TOKEN = "ZXlKallXTmxjblFpT2lKMFpYTjBJbjA9"


class NodeInstallKeyCommandTests(unittest.TestCase):
    def tearDown(self):
        update_channel.set_current_branch(update_channel.STABLE_BRANCH)

    def test_stable_channel_has_no_branch_env(self):
        command = build_install_command(TOKEN)
        self.assertEqual(
            command,
            f"bash <(curl -fsSL {update_channel.installer_url()}) {TOKEN}",
        )
        self.assertIn("/main/install.sh", command)
        self.assertNotIn("MON_BRANCH", command)

    def test_dev_channel_is_propagated_to_installer(self):
        update_channel.set_current_branch(update_channel.DEV_BRANCH)
        command = build_install_command(TOKEN)
        self.assertTrue(command.startswith("MON_BRANCH=dev bash <(curl -fsSL "))
        self.assertIn("/dev/install.sh", command)
        self.assertTrue(command.endswith(TOKEN))


if __name__ == "__main__":
    unittest.main()
