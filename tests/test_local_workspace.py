from types import SimpleNamespace

import httpx
import pytest

from paoding import local_workspace


@pytest.mark.parametrize("state", ["ready", "older_release", "database_down", "other_service"])
def test_launcher_reuses_only_a_ready_workspace(tmp_path, monkeypatch, state):
    def health(request):
        if request.url.path == "/health/live":
            data = {"status": "ok", "version": "0.2.0"}
            if state not in {"other_service", "older_release"}:
                data["service"] = "paoding"
            return httpx.Response(200, json=data)
        if request.url.path == "/openapi.json":
            title = "庖丁 PaoDing Engine" if state == "older_release" else "Other API"
            return httpx.Response(200, json={"info": {"title": title}})
        assert request.url.path == "/health/ready"
        return httpx.Response(503 if state == "database_down" else 200)

    settings = SimpleNamespace(host="::", port=8123, data_dir=tmp_path)
    monkeypatch.setattr(local_workspace, "Settings", lambda: settings)
    client = httpx.Client(base_url="http://[::1]:8123", transport=httpx.MockTransport(health))
    monkeypatch.setattr(local_workspace.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(local_workspace.subprocess, "Popen", lambda *args: pytest.fail("spawned"))
    opened = []
    monkeypatch.setattr(local_workspace.webbrowser, "open_new_tab", opened.append)

    if state in {"ready", "older_release"}:
        local_workspace.open_workspace()
        assert opened == ["http://[::1]:8123/library"]
    else:
        with pytest.raises(RuntimeError, match="数据库" if state == "database_down" else "端口"):
            local_workspace.open_workspace()
        assert not opened
