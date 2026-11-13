"""Доставка образа на ноду с плохим каналом и забитым диском.

Нода — настоящий SSH-сервер в процессе: SFTP пишет в каталог теста, команды ноды
(уборка, sha256sum, docker load, запуск) подставные. Проверяется то, ради чего
доставка идёт сеансами: обрыв продолжается с места обрыва, а не с нуля; на
забитый диск архив не заливается; сорвавшиеся загрузка и запуск не выдаются за
успех; к серверу с SOCKS5-прокси панель и по SSH ходит через него.
"""

import asyncio
import hashlib
import ipaddress
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

import asyncssh

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import node_image_delivery as module  # noqa: E402
from app.services.node_image_cache import NodeImageArchive  # noqa: E402
from app.services.ssh_target import SSHTarget  # noqa: E402

CHUNK = 64 * 1024
ARCHIVE_SIZE = 16 * CHUNK
IMAGE_SIZE = 300 * module.MIB
NO_SPACE_LINE = "write /var/lib/docker/tmp/layer: no space left on device"


class FakeNode:
    def __init__(self, root: Path):
        self.root = root
        (root / "var" / "tmp").mkdir(parents=True)
        (root / "tmp").mkdir()
        self.free_mb = 5000
        self.reclaimable_mb = 0  # сколько вернёт уборка образов без тега
        self.commands: list[str] = []
        self.kept_images: set[str] = set()  # какой образ уборке велено не трогать
        self.connections = 0
        self.received = 0
        self.drop_at: Optional[int] = None  # оборвать связь, приняв столько байт
        self.drop_every_session = False
        self.load_exit = 0
        self.start_exit = 0
        self.loaded_sha256: Optional[str] = None

    def local(self, remote_path: str) -> Path:
        return self.root / remote_path.lstrip("/")

    def takes_block(self, size: int) -> bool:
        """Принять блок заливки; False — связь на нём рвётся."""
        self.received += size
        if self.drop_every_session:
            return False
        if self.drop_at is not None and self.received >= self.drop_at:
            self.drop_at = None
            return False
        return True

    def run(self, process: asyncssh.SSHServerProcess) -> None:
        argv = shlex.split(process.command)
        script, args = argv[2], argv[4:]
        process.exit(self._dispatch(script, args, process))

    def _dispatch(self, script: str, args: list[str], process: asyncssh.SSHServerProcess) -> int:
        if script == module._CLEANUP_SCRIPT:
            return self._cleanup(args, process)
        archive = self.local(args[0]) if args else None
        if "sha256sum" in script:
            self.commands.append("sha256")
            if archive.exists():
                process.stdout.write(hashlib.sha256(archive.read_bytes()).hexdigest() + "\n")
            return 0
        if "docker load" in script:
            self.commands.append("load")
            if self.load_exit:
                process.stdout.write(NO_SPACE_LINE + "\n")
                archive.unlink()
                return self.load_exit
            self.loaded_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
            process.stdout.write("Loaded image: ghcr.io/joliz1337/monitoring-node-api:dev\n")
            return 0
        if "docker compose up -d" in script:
            self.commands.append("start")
            if self.start_exit:
                process.stdout.write("Error response from daemon: " + NO_SPACE_LINE + "\n")
                process.stdout.write("[node] контейнер агента не работает на доставленном образе\n")
            return self.start_exit
        if script.startswith("rm -f"):
            self.commands.append("rm")
            archive.unlink(missing_ok=True)
            return 0
        return 127

    def _cleanup(self, args: list[str], process: asyncssh.SSHServerProcess) -> int:
        archive_path, mode, legacy_path, _pattern, keep_image, _repos = args
        self.kept_images.add(keep_image)
        archive = self.local(archive_path)
        self.commands.append(f"cleanup:{mode}")
        before = self.free_mb
        self.free_mb += self.reclaimable_mb
        self.reclaimable_mb = 0
        if mode == "drop":
            archive.unlink(missing_ok=True)
        self.local(legacy_path).unlink(missing_ok=True)
        for other in archive.parent.glob(f"{module.REMOTE_PREFIX}*.tar.gz"):
            if other != archive:
                other.unlink()
        have = archive.stat().st_size if archive.exists() else 0
        process.stdout.write(
            f"MON_DISK before={before} docker={self.free_mb} upload={self.free_mb} same=1 have={have}\n"
        )
        return 0


def sshd_for(node: FakeNode):
    class Sshd(asyncssh.SSHServer):
        def connection_made(self, conn):
            node.connections += 1

        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return (username, password) == ("root", "pw")

    return Sshd


def sftp_for(node: FakeNode):
    class NodeSftp(asyncssh.SFTPServer):
        def __init__(self, chan):
            super().__init__(chan, chroot=str(node.root))

        def write(self, file_obj, offset, data):
            if not node.takes_block(len(data)):
                self.channel.get_connection().abort()
                raise asyncssh.SFTPConnectionLost("link dropped")
            return super().write(file_obj, offset, data)

    return NodeSftp


class Socks5Proxy:
    """SOCKS5 без аутентификации: запоминает, куда через него подключались."""

    def __init__(self):
        self.destinations: list[tuple[str, int]] = []
        self._tasks: set[asyncio.Task] = set()
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._server.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            _version, methods = await reader.readexactly(2)
            await reader.readexactly(methods)
            writer.write(b"\x05\x00")
            _version, _command, _reserved, address_type = await reader.readexactly(4)
            if address_type == 1:
                host = str(ipaddress.ip_address(await reader.readexactly(4)))
            else:
                host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
            port = int.from_bytes(await reader.readexactly(2), "big")
            self.destinations.append((host, port))
            upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
            writer.write(b"\x05\x00\x00\x01" + bytes(6))
            await asyncio.gather(
                self._pipe(reader, upstream_writer), self._pipe(upstream_reader, writer),
            )
        except (asyncio.IncompleteReadError, OSError):
            pass
        finally:
            writer.close()
            self._tasks.discard(task)

    @staticmethod
    async def _pipe(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()


class DeliverySessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.node = FakeNode(base / "node")

        tar = base / "node-api-dev.tar.gz"
        tar.write_bytes(os.urandom(ARCHIVE_SIZE))
        self.archive = NodeImageArchive(tar, hashlib.sha256(tar.read_bytes()).hexdigest(), IMAGE_SIZE)
        self.remote_tar = self.node.local(module.remote_archive_path(self.archive.sha256))

        for patcher in (
            mock.patch.object(module, "ensure_image", mock.AsyncMock(return_value=self.archive)),
            mock.patch.object(module, "SSH_CONNECT_RETRY_DELAY", 0),
            mock.patch.object(module, "UPLOAD_CHUNK", CHUNK),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        acceptor = await asyncssh.create_server(
            sshd_for(self.node), "127.0.0.1", 0,
            server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
            process_factory=self.node.run,
            sftp_factory=sftp_for(self.node),
        )
        self.addAsyncCleanup(acceptor.wait_closed)
        self.addCleanup(acceptor.close)
        self.port = acceptor.sockets[0].getsockname()[1]

    async def deliver(self, socks5_proxy: Optional[str] = None) -> list[dict]:
        target = SSHTarget(host="127.0.0.1", port=self.port, password="pw", socks5_proxy=socks5_proxy)
        return [event async for event in module.deliver_image(target, "dev")]

    @staticmethod
    def log(events: list[dict]) -> str:
        return "\n".join(e["line"] for e in events if e["type"] == "log")

    async def test_delivery_cleans_before_upload_and_after_start(self):
        self.node.local(module.LEGACY_REMOTE_TAR).write_bytes(b"half an image from an older panel")
        stale = self.remote_tar.with_name(f"{module.REMOTE_PREFIX}0123456789abcdef.tar.gz")
        stale.write_bytes(b"another image")
        self.node.reclaimable_mb = 3300

        events = await self.deliver()

        self.assertEqual(events[-1], {"type": "done", "message": "Образ доставлен, нода поднята"})
        self.assertEqual(self.node.commands, ["cleanup:keep", "sha256", "load", "start", "cleanup:drop"])
        self.assertEqual(self.node.loaded_sha256, self.archive.sha256)
        # Образы ноды под другими тегами уборка снимает, доставляемый — нет
        self.assertEqual(self.node.kept_images, {"ghcr.io/joliz1337/monitoring-node-api:dev"})
        self.assertIn("Уборка освободила 3300 МБ, на диске ноды свободно 8300 МБ", self.log(events))
        leftovers = [self.remote_tar, stale, self.node.local(module.LEGACY_REMOTE_TAR)]
        self.assertEqual([path.exists() for path in leftovers], [False, False, False])

    async def test_dropped_upload_continues_instead_of_starting_over(self):
        self.node.drop_at = 9 * CHUNK + 1

        events = await self.deliver()

        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(self.node.connections, 2)
        self.assertEqual(self.node.loaded_sha256, self.archive.sha256)
        self.assertIn("переподключаюсь", self.log(events))
        self.assertIn("докачиваю остальное", self.log(events))
        # Оборванный кусок и тот, в котором кончился файл, идут заново — не весь архив
        self.assertLessEqual(self.node.received, ARCHIVE_SIZE + 2 * CHUNK)

    async def test_archive_left_by_previous_job_is_not_uploaded_again(self):
        self.remote_tar.write_bytes(self.archive.path.read_bytes())

        events = await self.deliver()

        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(self.node.received, 0)
        self.assertIn("заливка не нужна", self.log(events))
        self.assertEqual(self.node.commands, ["cleanup:keep", "sha256", "load", "start", "cleanup:drop"])

    async def test_partial_archive_of_previous_job_is_completed(self):
        half = self.archive.path.read_bytes()[: 8 * CHUNK]
        self.remote_tar.write_bytes(half)

        events = await self.deliver()

        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(self.node.loaded_sha256, self.archive.sha256)
        self.assertEqual(self.node.received, ARCHIVE_SIZE - 7 * CHUNK)

    async def test_damaged_partial_archive_is_uploaded_from_scratch(self):
        self.remote_tar.write_bytes(os.urandom(8 * CHUNK))

        events = await self.deliver()

        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(self.node.loaded_sha256, self.archive.sha256)
        self.assertIn("заливаю заново с начала", self.log(events))

    async def test_full_disk_stops_delivery_before_upload(self):
        self.node.free_mb = 120

        events = await self.deliver()

        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("На диске ноды мало места: свободно 120 МБ", events[-1]["message"])
        self.assertEqual(self.node.received, 0)
        self.assertEqual(self.node.commands, ["cleanup:keep"])

    async def test_space_freed_by_cleanup_lets_delivery_through(self):
        self.node.free_mb = 0
        self.node.reclaimable_mb = 4700

        events = await self.deliver()

        self.assertEqual(events[-1]["type"], "done")

    async def test_failed_load_names_the_reason_and_leaves_no_archive(self):
        self.node.load_exit = 1

        events = await self.deliver()

        self.assertEqual(events[-1], {
            "type": "error", "message": f"docker load не удался (код 1): {NO_SPACE_LINE}",
        })
        self.assertNotIn("start", self.node.commands)
        self.assertFalse(self.remote_tar.exists())

    async def test_failed_start_is_not_reported_as_success(self):
        self.node.start_exit = 1

        events = await self.deliver()

        # В ошибке — и причина от docker, и итоговая строка команды
        self.assertEqual(events[-1], {
            "type": "error",
            "message": "Не удалось поднять ноду на образе (код 1): "
                       f"Error response from daemon: {NO_SPACE_LINE} / "
                       "[node] контейнер агента не работает на доставленном образе",
        })
        self.assertFalse(any(e["type"] == "done" for e in events))
        # Нода осталась на прежнем образе — убирать его и архив рано
        self.assertNotIn("cleanup:drop", self.node.commands)

    async def test_link_that_never_moves_forward_gives_up(self):
        self.node.drop_every_session = True

        events = await self.deliver()

        self.assertEqual(events[-1]["type"], "error")
        self.assertTrue(events[-1]["message"].startswith("Ошибка SSH: "))
        self.assertEqual(self.node.connections, module.STALLED_SESSIONS_LIMIT)

    async def test_server_behind_socks5_proxy_is_reached_through_it(self):
        proxy = Socks5Proxy()
        proxy_port = await proxy.start()
        self.addAsyncCleanup(proxy.stop)

        events = await self.deliver(socks5_proxy=f"127.0.0.1:{proxy_port}")

        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(proxy.destinations, [("127.0.0.1", self.port)])
        self.assertIn(f"установлен через SOCKS5 127.0.0.1:{proxy_port}", self.log(events))


class ResumeOffsetTests(unittest.TestCase):
    def test_chunk_holding_the_end_of_remote_file_is_sent_again(self):
        chunk = module.UPLOAD_CHUNK
        self.assertEqual(module.resume_offset(3 * chunk + 10, 10 * chunk), 3 * chunk)
        # Ровно на границе куска: дыра могла остаться в предыдущем
        self.assertEqual(module.resume_offset(3 * chunk, 10 * chunk), 2 * chunk)
        self.assertEqual(module.resume_offset(10 * chunk, 10 * chunk), 9 * chunk)

    def test_missing_or_foreign_file_starts_from_zero(self):
        chunk = module.UPLOAD_CHUNK
        self.assertEqual(module.resume_offset(0, 10 * chunk), 0)
        self.assertEqual(module.resume_offset(11 * chunk, 10 * chunk), 0)


class DiskReportTests(unittest.TestCase):
    def test_report_is_found_among_other_output(self):
        disk = module.parse_disk_report(
            "Deleted Images:\nMON_DISK before=120 docker=3420 upload=3420 same=1 have=1048576\n"
        )
        self.assertEqual(disk, module.NodeDisk(
            freed_mb=3300, docker_free_mb=3420, upload_free_mb=3420, same_disk=True, archive_bytes=1048576,
        ))

    def test_node_without_docker_or_df_gives_no_report(self):
        self.assertIsNone(module.parse_disk_report("MON_DISK before= docker= upload=5000 same=1 have=0\n"))

    def test_one_disk_must_hold_both_the_archive_and_the_image(self):
        disk = module.NodeDisk(0, docker_free_mb=600, upload_free_mb=600, same_disk=True, archive_bytes=0)
        self.assertIsNone(module.space_shortage(disk, 100 * module.MIB, 300 * module.MIB))
        self.assertIn("свободно 600 МБ, образу нужно около 700 МБ",
                      module.space_shortage(disk, 200 * module.MIB, 300 * module.MIB))

    def test_separate_disks_are_checked_separately(self):
        disk = module.NodeDisk(0, docker_free_mb=5000, upload_free_mb=250, same_disk=False, archive_bytes=0)
        self.assertIn(f"В {module.REMOTE_DIR} на ноде мало места",
                      module.space_shortage(disk, 200 * module.MIB, 300 * module.MIB))
        tight_docker = module.NodeDisk(0, docker_free_mb=400, upload_free_mb=5000, same_disk=False, archive_bytes=0)
        self.assertIn("На диске Docker ноды мало места",
                      module.space_shortage(tight_docker, 200 * module.MIB, 300 * module.MIB))


if __name__ == "__main__":
    unittest.main()
