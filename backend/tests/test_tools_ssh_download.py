from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools import ssh_download_file


class _FakeStat:
    def __init__(self, st_size: int = 0) -> None:
        self.st_size = st_size


class _FakeSFTP:
    def __init__(self) -> None:
        self.directories = {"/", "/tmp"}
        self.files: dict[str, bytes] = {}
        self.get_calls: list[tuple[str, str]] = []
        self.closed = False

    def stat(self, path: str) -> _FakeStat:
        if path in self.directories:
            return _FakeStat(0)
        if path in self.files:
            return _FakeStat(len(self.files[path]))
        raise OSError(f"not found: {path}")

    def get(self, remote_path: str, local_path: str) -> None:
        content = self.files[remote_path]
        Path(local_path).write_bytes(content)
        self.get_calls.append((remote_path, local_path))

    def close(self) -> None:
        self.closed = True


class _FakeSSHClient:
    def __init__(self, sftp: _FakeSFTP) -> None:
        self._sftp = sftp
        self.closed = False

    def open_sftp(self) -> _FakeSFTP:
        return self._sftp

    def close(self) -> None:
        self.closed = True


class SshDownloadFileToolTest(unittest.TestCase):
    def test_downloads_file_and_creates_local_directories(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            fake_sftp = _FakeSFTP()
            fake_sftp.files["/tmp/demo.txt"] = b"hello ssh download"
            fake_client = _FakeSSHClient(fake_sftp)
            target_file = Path(temp_dir) / "downloads" / "demo.txt"

            with patch("tools._paramiko_connect", return_value=fake_client):
                result = ssh_download_file.invoke(
                    {
                        "remote_path": "/tmp/demo.txt",
                        "local_path": str(target_file),
                    }
                )

            self.assertTrue(target_file.exists())
            self.assertEqual(target_file.read_text(encoding="utf-8"), "hello ssh download")

        self.assertIn("Downloaded file over SSH/SFTP.", result)
        self.assertIn("remote_path: /tmp/demo.txt", result)
        self.assertEqual(len(fake_sftp.get_calls), 1)
        self.assertTrue(fake_sftp.closed)
        self.assertTrue(fake_client.closed)

    def test_rejects_overwrite_when_local_file_exists(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            target_file = Path(temp_dir) / "demo.txt"
            target_file.write_text("existing", encoding="utf-8")

            result = ssh_download_file.invoke(
                {
                    "remote_path": "/tmp/demo.txt",
                    "local_path": str(target_file),
                    "overwrite": False,
                }
            )

        self.assertEqual(result, f"Error: local file already exists: {target_file}")

    def test_rejects_path_outside_workdir(self) -> None:
        escaped_path = str(Path(__file__).resolve().parents[2] / "escape.txt")
        result = ssh_download_file.invoke(
            {
                "remote_path": "/tmp/demo.txt",
                "local_path": escaped_path,
                "cwd": "tests",
            }
        )

        self.assertIn("Error: Path escapes working directory:", result)


if __name__ == "__main__":
    unittest.main()
