import httpx


async def test_health_is_public(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_missing_api_key_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.get("/_test/protected")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_wrong_api_key_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.get("/_test/protected", headers={"X-API-Key": "wrong"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_valid_api_key_is_accepted(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/_test/protected", headers=auth_headers)

    assert response.status_code == 200
