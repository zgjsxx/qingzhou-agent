from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools import ssh_upload_file


class _FakeStat:
    def __init__(self, st_size: int = 0) -> None:
        self.st_size = st_size


class _FakeSFTP:
    def __init__(self) -> None:
        self.directories = {"/", "/tmp"}
        self.files: dict[str, int] = {}
        self.put_calls: list[tuple[str, str]] = []
        self.closed = False

    def stat(self, path: str) -> _FakeStat:
        if path in self.directories:
            return _FakeStat(0)
        if path in self.files:
            return _FakeStat(self.files[path])
        raise OSError(f"not found: {path}")

    def mkdir(self, path: str) -> None:
        self.directories.add(path)

    def put(self, local_path: str, remote_path: str) -> None:
        size = Path(local_path).stat().st_size
        self.files[remote_path] = size
        self.put_calls.append((local_path, remote_path))

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


class SshUploadFileToolTest(unittest.TestCase):
    def test_uploads_file_and_creates_remote_directories(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            local_file = Path(temp_dir) / "demo.txt"
            local_file.write_text("hello ssh upload", encoding="utf-8")

            fake_sftp = _FakeSFTP()
            fake_client = _FakeSSHClient(fake_sftp)

            with patch("tools.registry._paramiko_connect", return_value=fake_client):
                result = ssh_upload_file.invoke(
                    {
                        "local_path": str(local_file),
                        "remote_path": "/tmp/upload/demo.txt",
                    }
                )

        self.assertIn("Uploaded file over SSH/SFTP.", result)
        self.assertIn("remote_path: /tmp/upload/demo.txt", result)
        self.assertIn("/tmp/upload", fake_sftp.directories)
        self.assertEqual(len(fake_sftp.put_calls), 1)
        self.assertTrue(fake_sftp.closed)
        self.assertTrue(fake_client.closed)

    def test_rejects_overwrite_when_remote_file_exists(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            local_file = Path(temp_dir) / "demo.txt"
            local_file.write_text("hello ssh upload", encoding="utf-8")

            fake_sftp = _FakeSFTP()
            fake_sftp.files["/tmp/demo.txt"] = 4
            fake_client = _FakeSSHClient(fake_sftp)

            with patch("tools.registry._paramiko_connect", return_value=fake_client):
                result = ssh_upload_file.invoke(
                    {
                        "local_path": str(local_file),
                        "remote_path": "/tmp/demo.txt",
                        "overwrite": False,
                    }
                )

        self.assertEqual(result, "Error: remote file already exists: /tmp/demo.txt")
        self.assertEqual(fake_sftp.put_calls, [])

    def test_rejects_missing_local_file(self) -> None:
        result = ssh_upload_file.invoke(
            {
                "local_path": "backend/tests/does-not-exist.txt",
                "remote_path": "/tmp/demo.txt",
                "cwd": "",
            }
        )

        self.assertIn("Error: local file does not exist:", result)


if __name__ == "__main__":
    unittest.main()
