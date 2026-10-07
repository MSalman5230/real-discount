import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monitor_lock import MonitorLock

ROOT = Path(__file__).resolve().parents[1]
ROOT_ON_LINUX = sys.platform == "linux" and os.geteuid() == 0


class LockPermissionTests(unittest.TestCase):
    def test_unwritable_lock_reports_data_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = MonitorLock(Path(directory) / ".telegram-monitor.lock")
            with patch.object(Path, "open", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(ValueError, "existing lock file.*writable"):
                    lock.__enter__()
            self.assertIsNone(lock.file)

    def test_unwritable_data_directory_reports_data_permissions(self):
        lock = MonitorLock("unwritable-data/.telegram-monitor.lock")
        with patch.object(Path, "mkdir", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(ValueError, "Docker's /data mount must be read/write"):
                lock.__enter__()
        self.assertIsNone(lock.file)


@unittest.skipUnless(ROOT_ON_LINUX, "Requires Linux root to verify ownership and privilege dropping")
class DockerStartupTests(unittest.TestCase):
    def run_startup(self, data, check, *, uid=None, gid=None):
        # Replace only the exec target with a probe. Initialization and privilege
        # dropping run in a real child process, leaving the test process intact.
        code = ("import os, sys; import docker_entrypoint; "
                "original_exec = os.execv; "
                "os.environ['STARTUP_PID'] = str(os.getpid()); "
                "os.execv = lambda executable, arguments: original_exec("
                "executable, [executable, '-c', " + json.dumps(check) + "]); "
                "sys.argv = ['docker_entrypoint.py', 'watch']; ")
        if uid is not None:
            code += f"os.setgroups([]); os.setgid({gid}); os.setuid({uid}); "
        code += "docker_entrypoint.main()"
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                              env={**os.environ, "REAL_DISCOUNT_DATA_DIR": str(data)},
                              capture_output=True, text=True, timeout=15)

    def test_root_owned_state_is_writable_after_dropping_privileges(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            parent.chmod(0o755)
            data = parent / "data"
            data.mkdir(mode=0o700)
            (data / ".telegram-monitor.lock").write_bytes(b"0")
            (data / ".telegram-monitor.lock").chmod(0o400)
            results = data / "results"
            results.mkdir()
            checkpoint = results / "telegram-read-state.json"
            checkpoint.write_text('{"version":1,"last_message_id":123}')
            checkpoint.chmod(0o400)
            with sqlite3.connect(data / "telegram-user.session") as session:
                session.execute("CREATE TABLE probe (value TEXT)")
                session.execute("INSERT INTO probe VALUES ('saved login')")
            (data / "telegram-user.session").chmod(0o600)
            results.chmod(0o500)
            outside = parent / "outside"
            outside.mkdir(mode=0o700)
            secret = outside / "private"
            secret.write_text("unchanged")
            secret.chmod(0o400)
            (data / "external-directory").symlink_to(outside, target_is_directory=True)
            (data / "external-file").symlink_to(secret)
            outside_info = [(item.stat().st_uid, item.stat().st_gid, item.stat().st_mode)
                            for item in (outside, secret)]
            check = """import json, os, sqlite3
from pathlib import Path
from monitor_lock import MonitorLock
assert (os.getuid(), os.getgid()) == (10001, 10001)
assert os.getgroups() == []
assert os.getpid() == int(os.environ['STARTUP_PID'])
data = Path(os.environ['REAL_DISCOUNT_DATA_DIR'])
with MonitorLock(data / '.telegram-monitor.lock'):
    with sqlite3.connect(data / 'telegram-user.session') as session:
        assert session.execute('SELECT value FROM probe').fetchone()[0] == 'saved login'
        session.execute("UPDATE probe SET value = 'saved login'")
    state = data / 'results' / 'telegram-read-state.json'
    assert json.loads(state.read_text())['last_message_id'] == 123
    state.write_text(state.read_text())
    (data / 'results' / 'probe').write_text('writable')
"""
            for _ in range(2):
                result = self.run_startup(data, check)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(outside_info,
                             [(item.stat().st_uid, item.stat().st_gid, item.stat().st_mode)
                              for item in (outside, secret)])
            self.assertTrue((data / "external-directory").is_symlink())
            self.assertTrue((data / "external-file").is_symlink())
            self.assertEqual(secret.read_text(), "unchanged")

    def test_explicit_nonroot_user_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            parent.chmod(0o755)
            data = parent / "data"
            data.mkdir(mode=0o700)
            os.chown(data, 10002, 10002)
            result = self.run_startup(data, """import os
from pathlib import Path
from monitor_lock import MonitorLock
assert (os.getuid(), os.getgid()) == (10002, 10002)
with MonitorLock(Path(os.environ['REAL_DISCOUNT_DATA_DIR']) / '.telegram-monitor.lock'):
    pass
""", uid=10002, gid=10002)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(data.stat().st_uid, 10002)

    def test_unusable_mount_fails_before_starting_monitor(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "file-instead-of-directory"
            data.write_text("unchanged")
            result = self.run_startup(data, "raise RuntimeError('Monitor should not start')")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Error preparing container startup", result.stderr)
            self.assertIn("mounted read/write and writable by UID/GID 10001:10001", result.stderr)
            self.assertNotIn("Monitor should not start", result.stderr)
            self.assertEqual(data.read_text(), "unchanged")

    def test_help_arguments_reach_the_monitor(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            parent.chmod(0o755)
            data = parent / "data"
            result = subprocess.run([sys.executable, str(ROOT / "docker_entrypoint.py"), "watch", "--help"],
                                    cwd=ROOT, env={**os.environ, "REAL_DISCOUNT_DATA_DIR": str(data)},
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--poll-seconds", result.stdout)
            self.assertEqual(data.stat().st_uid, 10001)


if __name__ == "__main__":
    unittest.main()
