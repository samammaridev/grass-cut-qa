"""SafeguardClient tests over httpx.MockTransport — verified live behaviors mocked exactly."""

import httpx
import pytest

from gcqa.api_client import AuthFailed, OrderNotFound, SafeguardClient
from gcqa.config import ApiCredentials

CREDS = ApiCredentials(
    auth_url="https://auth.test/json", auth_user="poc", auth_password="pw",
    api_base="https://api.test", image_base="https://img.test",
)
TOKEN_BODY = {"access_token": "tok1", "token_type": "Bearer", "expires_in": 36000}


def make_client(handler) -> SafeguardClient:
    return SafeguardClient(CREDS, transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_token_cached_across_calls():
    calls = {"auth": 0}

    async def handler(request):
        if request.url.host == "auth.test":
            calls["auth"] += 1
            return httpx.Response(200, json=TOKEN_BODY)
        assert request.headers["Authorization"] == "Bearer tok1"
        return httpx.Response(200, json={"items": []})

    async with make_client(handler) as c:
        await c.get_images("1")
        await c.get_images("2")
    assert calls["auth"] == 1


@pytest.mark.asyncio
async def test_403_refreshes_once_and_retries():
    state = {"auth": 0, "api": 0}

    async def handler(request):
        if request.url.host == "auth.test":
            state["auth"] += 1
            return httpx.Response(200, json={**TOKEN_BODY, "access_token": f"tok{state['auth']}"})
        state["api"] += 1
        if request.headers["Authorization"] == "Bearer tok1":
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(200, json={"ok": True})

    async with make_client(handler) as c:
        data = await c.get_luggage("500119014")
    assert data == {"ok": True}
    assert state["auth"] == 2 and state["api"] == 2


@pytest.mark.asyncio
async def test_500_marker_raises_order_not_found_without_retry():
    state = {"api": 0}

    async def handler(request):
        if request.url.host == "auth.test":
            return httpx.Response(200, json=TOKEN_BODY)
        state["api"] += 1
        return httpx.Response(500, text="failed workOrderClient.GetWorkOrderInfo")

    async with make_client(handler) as c:
        with pytest.raises(OrderNotFound):
            await c.get_luggage("999999999")
    assert state["api"] == 1                     # not-found is never retried


@pytest.mark.asyncio
async def test_bad_credentials_fail_fast():
    async def handler(request):
        return httpx.Response(401)

    async with make_client(handler) as c:
        with pytest.raises(AuthFailed):
            await c.get_luggage("1")


@pytest.mark.asyncio
async def test_download_all_isolates_failures(tmp_path):
    async def handler(request):
        if request.url.host == "auth.test":
            return httpx.Response(200, json=TOKEN_BODY)
        if "bad" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, content=b"\xff\xd8jpegbytes")

    items = [{"guid": "good1", "webFileName": "s3/x/good1.jpg"},
             {"guid": "bad1", "webFileName": "s3/x/bad1.jpg"},
             {"guid": "good2", "webFileName": "s3/x/good2.jpg"}]
    async with make_client(handler) as c:
        stats = await c.download_all(items, tmp_path)
    assert stats.downloaded == 2 and stats.failed == ["bad1"]
    assert (tmp_path / "good1.jpg").exists() and not (tmp_path / "bad1.jpg").exists()
