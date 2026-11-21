import os
import stat
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from paoding.core.config import Settings
from paoding.services.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]


def assert_private_file(path: Path):
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        return

    import ntsecuritycon
    import win32api
    import win32con
    import win32security

    descriptor = win32security.GetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
    )
    control, _ = descriptor.GetSecurityDescriptorControl()
    assert control & win32security.SE_DACL_PROTECTED
    acl = descriptor.GetSecurityDescriptorDacl()
    assert acl is not None and acl.GetAceCount() == 1
    (kind, flags), access, sid = acl.GetAce(0)
    assert (kind, flags, access) == (
        win32security.ACCESS_ALLOWED_ACE_TYPE,
        0,
        ntsecuritycon.FILE_ALL_ACCESS,
    )
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        assert sid == user_sid
    finally:
        token.Close()


def migrate_test_database(url):
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = url
    command.upgrade(config, "head")


@pytest.fixture
def rt(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        data_dir=tmp_path / "data",
        vault_dir=tmp_path / "vault",
        drop_dir=tmp_path / "drop",
        models_file=ROOT / "config/models.yaml",
        demo_mode=True,
        _env_file=None,
    )
    settings.prepare_directories()
    migrate_test_database(settings.database_url)
    runtime = Runtime(settings)
    yield runtime
    runtime.db.engine.dispose()


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.md"
    path.write_text("# 测试案例\n先指出痛点，再展示解决步骤，用验证结果结束。", encoding="utf-8")
    return path
