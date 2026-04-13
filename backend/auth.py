"""
Optional token-based authentication middleware.

Set the CLAUDE_MULTI_API_TOKEN environment variable (or api_token in .env) to
enable auth.  When set, every request must supply the token either as:

  • HTTP:      Authorization: Bearer <token>
  • WebSocket: ?token=<token>  (query parameter)

Paths exempt from auth: /health, / (frontend index), /src/* (assets).

When api_token is empty (the default), all requests are allowed — suitable for
localhost-only deployments where network exposure is not a concern.
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


# Paths that are always accessible regardless of token.
_EXEMPT_PREFIXES = ("/health", "/static/", "/src/")
_EXEMPT_EXACT = {"/"}


class TokenAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, api_token: str):
        super().__init__(app)
        self._token = api_token

    async def dispatch(self, request: Request, call_next):
        # Auth disabled — short-circuit immediately
        if not self._token:
            return await call_next(request)

        path = request.url.path

        # Exempt paths
        if path in _EXEMPT_EXACT:
            return await call_next(request)
        for prefix in _EXEMPT_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        # WebSocket upgrade: token arrives as query param because the browser
        # WebSocket API cannot set custom headers.
        if request.headers.get("upgrade", "").lower() == "websocket":
            provided = request.query_params.get("token", "")
        else:
            auth_header = request.headers.get("authorization", "")
            if auth_header.lower().startswith("bearer "):
                provided = auth_header[7:]
            else:
                provided = ""

        if provided != self._token:
            return JSONResponse(
                {"detail": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)
