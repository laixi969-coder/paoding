"""Publish credential files atomically with access limited to the current user."""

import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from dotenv import set_key
from filelock import FileLock


def _protect_windows_path(path: Path, *, directory: bool = False) -> None:
    import ntsecuritycon
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    flags = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE if directory else 0
    acl = win32security.ACL()
    acl.AddAccessAllowedAceEx(acl.GetAclRevision(), flags, ntsecuritycon.FILE_ALL_ACCESS, user_sid)
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        acl,
        None,
    )


@contextmanager
def _private_replacement(path: Path) -> Iterator[Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    # dotenv creates its own replacement files beside the file it edits.
    with tempfile.TemporaryDirectory(prefix=".paoding-", dir=path.parent) as directory:
        staging = Path(directory)
        if os.name == "nt":
            _protect_windows_path(staging, directory=True)
        pending = staging / path.name
        pending.touch(mode=0o600)
        yield pending
        if os.name == "nt":
            # Keep the final file private even if its destination directory is shared.
            _protect_windows_path(pending)
        else:
            pending.chmod(0o600)
        with pending.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(pending, path)


def update_dotenv(values: dict[str, str]) -> None:
    path = Path(".env")
    with FileLock(".env.lock"), _private_replacement(path) as pending:
        if path.exists():
            shutil.copyfile(path, pending)
        for name, value in values.items():
            set_key(pending, name, value, quote_mode="always", encoding="utf-8")


def write_private_json(path: Path, data: dict) -> None:
    with _private_replacement(path) as pending:
        pending.write_text(json.dumps(data), encoding="utf-8")
