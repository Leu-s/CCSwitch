import pytest
from unittest.mock import patch

@pytest.fixture(scope="module")
def client(make_test_app):
    from backend.routers.tmux import router
    _, c = make_test_app(router, db_name="tmux")
    return c

def test_list_sessions(client):
    panes = [{"target": "main:0.0", "command": "claude"}]
    with patch("backend.services.tmux_service.list_panes", return_value=panes):
        resp = client.get("/api/tmux/sessions")
    assert resp.status_code == 200
    assert resp.json()[0]["target"] == "main:0.0"

def test_create_monitor(client):
    payload = {"name": "test", "pattern_type": "manual", "pattern": "main:0.0", "enabled": True}
    resp = client.post("/api/tmux/monitors", json=payload)
    assert resp.status_code == 201
    assert resp.json()["name"] == "test"

def test_list_monitors(client):
    resp = client.get("/api/tmux/monitors")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    assert len(resp.json()) >= 1

def test_delete_monitor(client):
    # Create one to delete
    payload = {"name": "to-delete", "pattern_type": "manual", "pattern": "x:0.0", "enabled": True}
    create_resp = client.post("/api/tmux/monitors", json=payload)
    mid = create_resp.json()["id"]
    del_resp = client.delete(f"/api/tmux/monitors/{mid}")
    assert del_resp.status_code == 204
