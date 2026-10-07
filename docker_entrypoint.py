"""Prepare mounted state, then run the monitor as the unprivileged app user."""

import os
import stat
import sys
from pathlib import Path

APP_UID = 10001
APP_GID = 10001


def prepare_data_directory(path, uid=APP_UID, gid=APP_GID):
    path = Path(path).resolve()
    if path == Path(path.anchor):
        raise ValueError("REAL_DISCOUNT_DATA_DIR must be a dedicated data directory.")
    path.mkdir(parents=True, exist_ok=True)

    def walk_error(error):
        raise error

    # Bind mounts hide the ownership set at image-build time. Repair existing
    # sessions, locks and checkpoints too, without following links out of /data.
    for directory, _, files in os.walk(path, onerror=walk_error, followlinks=False):
        for item in [Path(directory), *(Path(directory) / name for name in files)]:
            info = item.lstat()
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                continue
            # Operate on the opened inode; Linux cannot chmod a symlink.
            descriptor = os.open(item, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(descriptor)
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    continue
                if (info.st_uid, info.st_gid) != (uid, gid):
                    os.fchown(descriptor, uid, gid)
                required = stat.S_IRUSR | stat.S_IWUSR
                if stat.S_ISDIR(info.st_mode):
                    required |= stat.S_IXUSR
                mode = stat.S_IMODE(info.st_mode) | required
                if mode != stat.S_IMODE(info.st_mode):
                    os.fchmod(descriptor, mode)
            finally:
                os.close(descriptor)


def main():
    data_dir = Path(os.environ.get("REAL_DISCOUNT_DATA_DIR") or "/data")
    try:
        if os.geteuid() == 0:
            prepare_data_directory(data_dir)
            os.setgroups([])
            os.setgid(APP_GID)
            os.setuid(APP_UID)
        monitor = Path(__file__).with_name("telegram_monitor.py")
        os.execv(sys.executable, [sys.executable, str(monitor), *sys.argv[1:]])
    except (OSError, ValueError) as exc:
        sys.exit(f"Error preparing container startup: {exc}. Ensure {data_dir} is "
                 f"mounted read/write and writable by UID/GID {APP_UID}:{APP_GID}.")


if __name__ == "__main__":
    main()
