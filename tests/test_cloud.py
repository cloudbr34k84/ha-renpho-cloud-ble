"""Cloud account: the async client against a mocked Renpho API, and the sensors."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.renpho import cloud_api
from custom_components.renpho.cloud_api import (
    API_BASE,
    RenphoAuthError,
    RenphoCloud,
    RenphoCloudError,
    RenphoRateLimited,
    _encrypt,
)
from custom_components.renpho.const import DOMAIN
from custom_components.renpho.diagnostics import async_get_config_entry_diagnostics

LATEST = {
    "users": {"1001": "Alex", "1002": "Sam"},
    "scales": [],
    "latest": {
        "1001": {"userId": "1001", "weight": 82.5, "bodyfat": 20.1, "bmi": 25.5, "visfat": 9, "timeStamp": 1792814122},
        "1002": {"userId": "1002", "weight": 61.2, "bodyfat": 27.0, "timeStamp": 1792810000},
    },
}


def ok(data) -> dict:
    return {"code": 101, "msg": "success", "data": _encrypt(data)["encryptData"]}


async def test_client_fetches_latest_per_user(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(f"{API_BASE}/{cloud_api.EP_LOGIN}", json=ok({"login": {"id": 1001, "token": "t", "accountName": "Alex"}}))
    aioclient_mock.post(f"{API_BASE}/{cloud_api.EP_DEVICES}", json=ok({"scale": [{"tableName": "measurements_info_17", "userIds": [1001], "count": 2}]}))
    aioclient_mock.post(f"{API_BASE}/{cloud_api.EP_FAMILY}", json=ok([{"userId": 1002, "nickName": "Sam"}]))
    # Body-composition endpoint answers for both users; records carry no userId, so the
    # queried uid is attached. One request URL serves both calls, so both users get the
    # same rows here — enough to check newest-record selection and user tagging.
    aioclient_mock.post(
        f"{API_BASE}/{cloud_api.EP_BODY_COMP}",
        json=ok([{"id": 1, "weight": 82.0, "timeStamp": 100}, {"id": 2, "weight": 82.5, "timeStamp": 200}]),
    )
    aioclient_mock.post(f"{API_BASE}/{cloud_api.EP_MEASUREMENTS}", json=ok([]))
    api = RenphoCloud(async_get_clientsession(hass), "a@b.c", "pw")
    data = await api.latest_measurements()
    assert data["users"] == {"1002": "Sam", "1001": "Alex"}
    assert data["latest"]["1001"]["weight"] == 82.5
    assert data["latest"]["1002"]["weight"] == 82.5
    # Login carries appVersion (the gateway picks its decryption key from it,
    # without it every login fails with -114); later calls add the token
    _, _, _, login_headers = aioclient_mock.mock_calls[0]
    assert login_headers == {"appVersion": cloud_api.APP_VERSION, "platform": "android"}
    _, _, _, headers = aioclient_mock.mock_calls[1]
    assert headers["token"] == "t" and headers["userId"] == "1001" and headers["appVersion"] == "6.6.0"


async def test_login_rejected(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(f"{API_BASE}/{cloud_api.EP_LOGIN}", json={"code": 50001, "msg": "password error"})
    api = RenphoCloud(async_get_clientsession(hass), "a@b.c", "bad")
    try:
        await api.login()
    except RenphoAuthError:
        pass
    else:
        raise AssertionError("expected RenphoAuthError")


async def test_cloud_sensors(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="cloud_1001", title="Renpho Cloud (a@b.c)",
        data={"source": "cloud", "email": "a@b.c", "password": "pw"},
    )
    entry.add_to_hass(hass)
    with patch.object(RenphoCloud, "latest_measurements", AsyncMock(return_value=LATEST)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert float(hass.states.get("sensor.renpho_cloud_alex_weight").state) == 82.5
    assert float(hass.states.get("sensor.renpho_cloud_alex_body_fat").state) == 20.1
    assert float(hass.states.get("sensor.renpho_cloud_alex_visceral_fat").state) == 9
    assert hass.states.get("sensor.renpho_cloud_alex_last_measurement").state.startswith("2026-10-24")
    assert float(hass.states.get("sensor.renpho_cloud_sam_weight").state) == 61.2
    assert hass.states.get("sensor.renpho_cloud_sam_bmi").state == "unknown"


async def test_cloud_auth_failure_starts_reauth(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="cloud_1001", data={"source": "cloud", "email": "a@b.c", "password": "pw"},
    )
    entry.add_to_hass(hass)
    with (
        patch.object(RenphoCloud, "latest_measurements", AsyncMock(side_effect=RenphoAuthError("x"))),
        patch.object(RenphoCloud, "login", AsyncMock(side_effect=RenphoAuthError("x"))),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == SOURCE_REAUTH for f in flows)


async def test_reauth_after_failed_setup(hass: HomeAssistant, caplog) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="cloud_1001", data={"source": "cloud", "email": "a@b.c", "password": "old"},
    )
    entry.add_to_hass(hass)
    with (
        patch.object(RenphoCloud, "latest_measurements", AsyncMock(side_effect=RenphoAuthError("x"))),
        patch.object(RenphoCloud, "login", AsyncMock(side_effect=RenphoAuthError("x"))),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    flow = next(f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == SOURCE_REAUTH)

    async def login(self):
        self.user_id, self.token = "1001", "t"

    with (
        patch.object(RenphoCloud, "login", login),
        patch.object(RenphoCloud, "latest_measurements", AsyncMock(return_value=LATEST)),
    ):
        result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"password": "new"})
        await hass.async_block_till_done()
    assert result["type"] == "abort" and result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new"
    assert entry.state.value == "loaded"
    assert float(hass.states.get("sensor.renpho_cloud_alex_weight").state) == 82.5
    assert "update listener" not in caplog.text


async def test_reauth_while_loaded_reloads_once(hass: HomeAssistant, caplog) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="cloud_1001", data={"source": "cloud", "email": "a@b.c", "password": "old"},
    )
    entry.add_to_hass(hass)
    fetch = AsyncMock(return_value=LATEST)

    async def login(self):
        self.user_id, self.token = "1001", "t"

    with patch.object(RenphoCloud, "latest_measurements", fetch), patch.object(RenphoCloud, "login", login):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "new"})
        await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert fetch.await_count == 2  # initial setup + exactly one reload
    assert entry.runtime_data.api._password == "new"
    assert "update listener" not in caplog.text


def cloud_entry(hass: HomeAssistant, account: str = "1001", email: str = "a@b.c") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=f"cloud_{account}", title=f"Renpho Cloud ({email})",
        data={"source": "cloud", "email": email, "password": "pw"},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.parametrize("newest_first", [True, False])
async def test_newest_record_found_whatever_the_sort_order(hass: HomeAssistant, newest_first: bool) -> None:
    records = [{"id": i, "weight": 70 + i / 10, "timeStamp": 1000 + i} for i in range(120)]
    if newest_first:
        records.reverse()
    pages = []

    async def fake_call(self, endpoint, body):
        pages.append(body["pageNum"])
        start = (body["pageNum"] - 1) * body["pageSize"]
        return records[start : start + body["pageSize"]]

    api = RenphoCloud(async_get_clientsession(hass), "a@b.c", "pw")
    with patch.object(RenphoCloud, "_call", fake_call):
        got = await api._pages(cloud_api.EP_BODY_COMP, "measurements_info_1", "1", everything=False)
        assert max(got, key=cloud_api._ts)["timeStamp"] == 1119
        assert pages == ([1] if newest_first else [1, 2, 3])  # newest-first stops after one page
        pages.clear()
        assert len(await api._pages(cloud_api.EP_BODY_COMP, "measurements_info_1", "1", everything=True)) == 120
        assert pages == [1, 2, 3]


async def test_rate_limit_backs_off(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(f"{API_BASE}/{cloud_api.EP_LOGIN}", status=429, headers={"Retry-After": "120"})
    api = RenphoCloud(async_get_clientsession(hass), "a@b.c", "pw")
    with pytest.raises(RenphoRateLimited) as err:
        await api.login()
    assert err.value.retry_after == 120

    entry = cloud_entry(hass)
    fetch = AsyncMock(side_effect=RenphoRateLimited(120))
    with patch.object(RenphoCloud, "latest_measurements", fetch):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert fetch.await_count == 1  # no immediate re-login retry on 429
    assert entry.state.value == "setup_retry"


async def test_unknown_error_gets_one_fresh_login(hass: HomeAssistant) -> None:
    entry = cloud_entry(hass)
    fetch = AsyncMock(side_effect=[RenphoCloudError("code=12345 msg=token invalid"), LATEST])
    with patch.object(RenphoCloud, "latest_measurements", fetch):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert fetch.await_count == 2
    assert entry.runtime_data.api.token is None  # cleared so the retry logged in again
    assert float(hass.states.get("sensor.renpho_cloud_alex_weight").state) == 82.5


async def test_two_family_accounts_do_not_collide(hass: HomeAssistant) -> None:
    first, second = cloud_entry(hass, "1001", "alex@x"), cloud_entry(hass, "1002", "sam@x")
    with patch.object(RenphoCloud, "latest_measurements", AsyncMock(return_value=LATEST)):
        assert await hass.config_entries.async_setup(first.entry_id)  # loads every renpho entry
        await hass.async_block_till_done()
    assert first.state.value == "loaded" and second.state.value == "loaded"
    assert hass.states.get("sensor.renpho_cloud_alex_weight") is not None
    assert hass.states.get("sensor.renpho_cloud_alex_weight_2") is not None


async def test_cloud_diagnostics_redacted(hass: HomeAssistant) -> None:
    entry = cloud_entry(hass)
    with patch.object(RenphoCloud, "latest_measurements", AsyncMock(return_value=LATEST)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    dumped = str(diag)
    assert "a@b.c" not in dumped and "'pw'" not in dumped
    assert "Alex" not in dumped and "1001" not in dumped
    assert diag["last_update_success"] is True and diag["user_count"] == 2


async def test_family_names_from_current_app_fields(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Family members come back with firstName/lastName/accountName, not nickName."""
    aioclient_mock.post(f"{API_BASE}/{cloud_api.EP_LOGIN}", json=ok({"login": {"id": 1001, "token": "t", "firstName": "Alex"}}))
    aioclient_mock.post(f"{API_BASE}/{cloud_api.EP_DEVICES}", json=ok({"scale": []}))
    aioclient_mock.post(
        f"{API_BASE}/{cloud_api.EP_FAMILY}",
        json=ok([
            {"id": 1002, "firstName": "Sam", "lastName": "S", "accountName": "sam@x"},
            {"id": 1003, "accountName": "Kid"},
            {"id": 1004},
        ]),
    )
    aioclient_mock.post(f"{API_BASE}/{cloud_api.EP_BODY_COMP}", json=ok([]))
    aioclient_mock.post(f"{API_BASE}/{cloud_api.EP_MEASUREMENTS}", json=ok([]))
    api = RenphoCloud(async_get_clientsession(hass), "a@b.c", "pw")
    data = await api.latest_measurements()
    assert data["users"] == {"1002": "Sam S", "1003": "Kid", "1004": "1004", "1001": "Alex"}


def test_app_version_stays_on_the_pre_8_key() -> None:
    """appVersion >= 8.0.0 makes the gateway use a key we don't have (-114 on every login)."""
    assert int(cloud_api.APP_VERSION.split(".")[0]) < 8


async def test_gateway_message_field_is_reported(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(
        f"{API_BASE}/{cloud_api.EP_LOGIN}",
        json={"code": -114, "message": "Decryption failed, please check the parameters or key"},
    )
    api = RenphoCloud(async_get_clientsession(hass), "a@b.c", "pw")
    with pytest.raises(RenphoAuthError) as err:
        await api.login()
    assert err.value.code == -114 and "Decryption failed" in str(err.value)


async def test_both_stores_merged_and_attributed(hass: HomeAssistant) -> None:
    """Classic and body-composition records are read and merged, owners come from bUserId."""
    classic = [
        {"id": 7, "weight": 80.0, "timeStamp": 100, "bodyfat": 20.0, "heartRate": 70},
        {"id": 8, "weight": 79.0, "timeStamp": 50, "invalidFlag": 1},
        {"id": 9, "weight": 61.0, "timeStamp": 120, "bUserId": 1002},
    ]
    body = [{"id": 70, "weight": 80.0, "timeStamp": 100, "bodyfat": 20.5, "smmMass": 33.0}]

    async def fake_call(self, endpoint, data):
        if endpoint == cloud_api.EP_DEVICES:
            return {"scale": [{"tableName": "t", "userIds": [1001], "count": 3}]}
        if endpoint == cloud_api.EP_FAMILY:
            return [{"id": 1002, "firstName": "Sam"}]
        if data["userIds"] != ["1001"]:
            return []  # Sam's own shard has nothing extra
        return classic if endpoint == cloud_api.EP_MEASUREMENTS else body

    api = RenphoCloud(async_get_clientsession(hass), "a@b.c", "pw")
    api.token, api.user_id, api.user_info = "t", "1001", {"firstName": "Alex"}
    with patch.object(RenphoCloud, "_call", fake_call):
        data = await api.latest_measurements()
    alex = data["latest"]["1001"]
    assert alex["smmMass"] == 33.0 and alex["heartRate"] == 70  # merged from both stores
    assert alex["bodyfat"] == 20.5  # body composition wins on overlap
    assert data["latest"]["1002"]["weight"] == 61.0  # owner from bUserId, not the queried id
    assert [r["timeStamp"] for r in data["records"]] == [100, 120]  # invalid record dropped, oldest first


async def test_no_recorder_no_import(hass: HomeAssistant) -> None:
    from .test_cloud_history import T0, morphoscan, payload

    entry = cloud_entry(hass)
    latest = AsyncMock(return_value=payload([morphoscan(T0, 95.0)]))
    with patch.object(RenphoCloud, "latest_measurements", latest):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert await entry.runtime_data.history.async_sync() == 0
    assert not any(call.kwargs.get("everything") for call in latest.await_args_list)
