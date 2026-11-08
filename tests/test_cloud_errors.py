"""Cloud wire errors and response variants, with encrypted test fixtures."""

from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.renpho import cloud_api as api
from .test_cloud import ok


@pytest.mark.parametrize("headers,expected", [({"Retry-After": "not-a-number"}, 900), ({}, 900)])
async def test_rate_limit_header_fallback(hass, aioclient_mock, headers, expected):
    aioclient_mock.post(f"{api.API_BASE}/{api.EP_LOGIN}", status=429, headers=headers)
    client = api.RenphoCloud(async_get_clientsession(hass), "a@b.c", "pw")
    with pytest.raises(api.RenphoRateLimited) as error:
        await client.login()
    assert error.value.retry_after == expected


@pytest.mark.parametrize("error", [aiohttp.ClientConnectionError("offline"), TimeoutError()])
async def test_network_error(hass, aioclient_mock, error):
    aioclient_mock.post(f"{api.API_BASE}/{api.EP_LOGIN}", exc=error)
    client = api.RenphoCloud(async_get_clientsession(hass), "a@b.c", "pw")
    with pytest.raises(api.RenphoCloudError):
        await client.login()


async def test_incomplete_login(hass, aioclient_mock):
    aioclient_mock.post(f"{api.API_BASE}/{api.EP_LOGIN}", json=ok({"login": {"id": 1}}))
    with pytest.raises(api.RenphoAuthError, match="no token"):
        await api.RenphoCloud(async_get_clientsession(hass), "a@b.c", "pw").login()


@pytest.mark.parametrize("code,error", [(401, api.RenphoAuthError), (500, api.RenphoCloudError)])
async def test_api_error_and_optional_family(hass, aioclient_mock, code, error):
    client = api.RenphoCloud(async_get_clientsession(hass), "a@b.c", "pw")
    aioclient_mock.post(f"{api.API_BASE}/{api.EP_DEVICES}", json={"code": code})
    with pytest.raises(error):
        await client._call(api.EP_DEVICES, {})
    aioclient_mock.post(f"{api.API_BASE}/{api.EP_FAMILY}", json={"code": code})
    if error is api.RenphoAuthError:
        with pytest.raises(api.RenphoAuthError):
            await client.family()
    else:
        assert await client.family() == {}


async def test_response_shapes_and_missing_optional_devices(hass):
    client = api.RenphoCloud(async_get_clientsession(hass), "a@b.c", "pw")
    client.token, client.user_id = "token", "1001"

    async def call(endpoint, body):
        if endpoint == api.EP_DEVICES:
            if body is None:
                raise api.RenphoCloudError("empty body rejected")
            return {"scale": [{"userIds": [1001]}]}  # no table, use account shard
        if endpoint == api.EP_FAMILY:
            return {"list": [{"userId": "1001", "nickName": "Alex"}]}
        if endpoint == api.EP_MEASUREMENTS:
            return {"measurements": [{"weight": 80, "timeStamp": "bad"}]}
        return {"weight": 81, "timeStamp": 200}

    with patch.object(client, "_call", call):
        result = await client.latest_measurements()
    assert result["latest"]["1001"]["weight"] == 81
    assert result["records"][0]["timeStamp"] == "bad"
    assert api._records({"unrelated": 3}) == []
    assert api._records(None) == []
    assert api.profile_goals({"weightGoalDate": "not-a-date"}, None) == {}


async def test_family_rate_limit_is_not_hidden(hass, aioclient_mock):
    client = api.RenphoCloud(async_get_clientsession(hass), "a@b.c", "pw")
    aioclient_mock.post(f"{api.API_BASE}/{api.EP_FAMILY}", status=429, headers={"Retry-After": "300"})
    with pytest.raises(api.RenphoRateLimited) as raised:
        await client.family()
    assert raised.value.retry_after == 300


@pytest.mark.parametrize("response", [[], "server error", {"code": 101, "data": "invalid ciphertext"}, ok([]), ok({"login": []})])
async def test_malformed_login_response_is_connection_error(hass, aioclient_mock, response):
    client = api.RenphoCloud(async_get_clientsession(hass), "a@b.c", "pw")
    aioclient_mock.post(f"{api.API_BASE}/{api.EP_LOGIN}", json=response)
    with pytest.raises(api.RenphoCloudError) as raised:
        await client.login()
    assert not isinstance(raised.value, api.RenphoAuthError)


async def test_invalid_json_is_connection_error(hass, aioclient_mock):
    client = api.RenphoCloud(async_get_clientsession(hass), "a@b.c", "pw")
    aioclient_mock.post(f"{api.API_BASE}/{api.EP_LOGIN}", text="<html>unavailable</html>")
    with pytest.raises(api.RenphoCloudError):
        await client.login()
