"""Tests for TokenAuthMiddleware."""
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from backend.auth import TokenAuthMiddleware


def homepage(request: Request):
    return PlainTextResponse("ok")


def health(request: Request):
    return PlainTextResponse("healthy")


def make_app(token: str) -> Starlette:
    app = Starlette(routes=[
        Route("/", homepage),
        Route("/health", health),
        Route("/api/data", homepage),
    ])
    app.add_middleware(TokenAuthMiddleware, api_token=token)
    return app


def test_no_token_configured_allows_all():
    client = TestClient(make_app(""))
    assert client.get("/api/data").status_code == 200


def test_health_exempt_always():
    client = TestClient(make_app("secret"))
    assert client.get("/health").status_code == 200


def test_root_exempt_always():
    client = TestClient(make_app("secret"))
    assert client.get("/").status_code == 200


def test_missing_token_returns_401():
    client = TestClient(make_app("secret"), raise_server_exceptions=False)
    assert client.get("/api/data").status_code == 401


def test_wrong_token_returns_401():
    client = TestClient(make_app("secret"), raise_server_exceptions=False)
    assert client.get("/api/data", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_correct_bearer_token_allowed():
    client = TestClient(make_app("secret"))
    assert client.get("/api/data", headers={"Authorization": "Bearer secret"}).status_code == 200
