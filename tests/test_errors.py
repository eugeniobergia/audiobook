import httpx
from fastapi import FastAPI


async def test_unknown_route_uses_error_shape(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/does-not-exist", headers=auth_headers)

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Not Found"}}


async def test_wrong_method_uses_error_shape(client: httpx.AsyncClient) -> None:
    response = await client.post("/health")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"
    assert response.headers["allow"] == "GET"


async def test_validation_error_includes_details(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/_test/protected?n=abc", headers=auth_headers)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"][0]["loc"] == ["query", "n"]


async def test_unexpected_error_does_not_leak_internals(
    app: FastAPI, auth_headers: dict[str, str]
) -> None:
    # raise_app_exceptions=False: let the app turn the exception into a 500
    # response instead of the test client re-raising it.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/_test/boom", headers=auth_headers)

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "An unexpected error occurred."}
    }
    assert "secret internals" not in response.text
