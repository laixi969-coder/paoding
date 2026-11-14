from fastapi.testclient import TestClient
from typer.testing import CliRunner

from paoding.api.app import create_app
from paoding.cli import app
from paoding.core.config import Settings


def test_blank_optional_paths_do_not_become_current_directory():
    settings = Settings(cookies_file="", workbuddy_dir="", _env_file=None)
    assert settings.cookies_file is None and settings.workbuddy_dir is None


def test_capture_page_and_bookmarklet_do_not_include_tokens(rt):
    with TestClient(create_app(rt)) as client:
        response = client.get("/capture")
        assert response.status_code == 200
        assert "const authRequired=false" in response.text
        assert response.headers["referrer-policy"] == "no-referrer"
    result = CliRunner().invoke(app, ["bookmarklet"])
    assert result.exit_code == 0 and "auto:true" in result.stdout
    assert "javascript:" in result.stdout and "document.title" in result.stdout
