import httpx

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
REFRESH_URL = "https://platform.claude.com/v1/oauth/token"

async def fetch_usage(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            USAGE_URL,
            headers={"Authorization": f"Bearer {access_token}"}
        )
        resp.raise_for_status()
        return resp.json()

async def refresh_access_token(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            REFRESH_URL,
            json={"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
        resp.raise_for_status()
        return resp.json()
