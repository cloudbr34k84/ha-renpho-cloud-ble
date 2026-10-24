"""Async Renpho Health cloud client.

Re-implementation of renpho-api 0.2.0 (MIT, used by DiscountDarcy/ha-renpho-health)
on aiohttp + cryptography so no extra PyPI requirements are needed. Every
request/response body is AES-128-ECB/PKCS7 JSON, base64 in ``encryptData``.
"""

from __future__ import annotations

import base64
from datetime import UTC, date, datetime
import json
import logging
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

API_BASE = "https://cloud.renpho.com"
_KEY = b"ed*wijdi$h6fe3ew"
# Load-bearing: Renpho's gateway picks the payload decryption key from the
# appVersion header. _KEY is the pre-8.0.0 key, so this must stay below 8.0.0,
# and it must be sent on login too, or every login fails with -114
# ("Decryption failed"). See danvaneijck/renpho-api PR #13.
APP_VERSION = "6.6.0"
PLATFORM = "android"
_SUCCESS = {0, "0", 101, "101", 200, "200", 20000, "20000"}
_SCALE_TYPES = [f"{i:02X}" for i in range(1, 0x15)]
_TABLE_SHARDS = 24
_PAGE_SIZE = 50
_MAX_PAGES = 100

EP_LOGIN = "renpho-aggregation/user/login"
EP_DEVICES = "renpho-aggregation/device/count"
EP_FAMILY = "RenphoHealth/centerUser/queryFamilyMemberList"
EP_MEASUREMENTS = "RenphoHealth/scale/queryAllMeasureDataList"
EP_BODY_COMP = "RenphoHealth/scale/queryBodyCompositionMeasureData"


class RenphoCloudError(Exception):
    """Generic cloud failure."""


class RenphoAuthError(RenphoCloudError):
    """Credentials rejected. ``code`` is Renpho's own result code, when it sent one."""

    def __init__(self, message: str, code: Any = None) -> None:
        super().__init__(message)
        self.code = code


class RenphoRateLimited(RenphoCloudError):
    """HTTP 429 — back off for ``retry_after`` seconds."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after:.0f}s")
        self.retry_after = retry_after


def _encrypt(obj: Any) -> dict[str, str]:
    raw = json.dumps(obj, separators=(",", ":")).encode() if obj is not None else b""
    padder = padding.PKCS7(128).padder()
    data = padder.update(raw) + padder.finalize()
    enc = Cipher(algorithms.AES(_KEY), modes.ECB()).encryptor()  # noqa: S305 - protocol-mandated
    return {"encryptData": base64.b64encode(enc.update(data) + enc.finalize()).decode()}


def _decrypt(b64: str) -> Any:
    dec = Cipher(algorithms.AES(_KEY), modes.ECB()).decryptor()  # noqa: S305
    padded = dec.update(base64.b64decode(b64)) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return json.loads((unpadder.update(padded) + unpadder.finalize()).decode())


def _message(result: dict) -> str:
    """The gateway's error text; current responses put it in ``message``."""
    return result.get("msg") or result.get("message") or ""


_LB_PER_KG = 2.20462262185


def _num(value: Any) -> float | None:
    """A positive number, or None (the profile uses 0 for "not set")."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def _goal_date(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC).date().isoformat()
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10]).isoformat()
        except ValueError:
            return None
    return None


def profile_goals(person: dict, latest_weight: float | None) -> dict[str, Any]:
    """Goals and body measurements from a login or family-member record, in kg / cm.

    Weight units follow the app's codes (1 kg, 2 lb); a height unit other than
    0 means inches, which is assumed for the waist and hip too.
    """
    lb = person.get("weightGoalUnit", person.get("weightUnit")) == 2
    to_kg = (lambda v: round(v / _LB_PER_KG, 2)) if lb else (lambda v: v)
    to_cm = (lambda v: round(v * 2.54, 1)) if person.get("heightUnit") not in (None, 0) else (lambda v: v)
    goal = _num(person.get("weightGoal")) or _num(person.get("currentGoalWeight"))
    out: dict[str, Any] = {
        "weight_goal": to_kg(goal) if goal else None,
        "weight_goal_date": _goal_date(person.get("weightGoalDate")),
        "body_fat_goal": _num(person.get("bodyfatGoal")),
        "starting_weight": to_kg(w) if (w := _num(person.get("initialWeight"))) else None,
        "starting_body_fat": _num(person.get("initialBodyfat")),
        "waist": to_cm(w) if (w := _num(person.get("waistline"))) else None,
        "hip": to_cm(h) if (h := _num(person.get("hip"))) else None,
    }
    if out["weight_goal"] and latest_weight:
        out["weight_to_goal"] = round(latest_weight - out["weight_goal"], 2)
    return {k: v for k, v in out.items() if v is not None}


def _ts(record: dict) -> int:
    try:
        return int(record.get("timeStamp") or record.get("time_stamp") or 0)
    except (TypeError, ValueError):
        return 0


def _display_name(person: dict) -> str | None:
    """Best name for a login or family-member record (field names vary by app version)."""
    full = " ".join(p for p in (person.get("firstName"), person.get("lastName")) if p)
    return person.get("nickName") or full or person.get("accountName") or person.get("name") or None


def _records(page: Any) -> list[dict]:
    if isinstance(page, list):
        return page
    if isinstance(page, dict):
        for key in ("list", "data", "records", "measurements"):
            if isinstance(page.get(key), list):
                return page[key]
        if "weight" in page:
            return [page]
    return []


class RenphoCloud:
    """Minimal read-only Renpho cloud client."""

    def __init__(self, session: aiohttp.ClientSession, email: str, password: str) -> None:
        self._session = session
        self._email = email.strip()
        self._password = password
        self.token: str | None = None
        self.user_id: str | None = None
        self.user_info: dict[str, Any] = {}
        self.family_records: dict[str, dict[str, Any]] = {}

    async def _post(self, endpoint: str, body: dict, *, auth: bool = True) -> dict:
        headers = {"appVersion": APP_VERSION, "platform": PLATFORM}
        if auth and self.token:
            headers |= {"token": self.token, "userId": str(self.user_id)}
        try:
            async with self._session.post(
                f"{API_BASE}/{endpoint}",
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 429:
                    try:
                        retry_after = float(resp.headers.get("Retry-After", 900))
                    except ValueError:
                        retry_after = 900.0
                    raise RenphoRateLimited(retry_after)
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise RenphoCloudError(f"{endpoint}: {err}") from err

    @staticmethod
    def _ok(result: dict) -> bool:
        return str(_message(result)).lower() == "success" or result.get("code") in _SUCCESS

    async def login(self) -> None:
        payload = {
            "questionnaire": {},
            "login": {
                "password": self._password,
                "areaCode": "US",
                "appRevision": APP_VERSION,
                "cellphoneType": "HomeAssistant",
                "systemType": "11",
                "email": self._email,
                "platform": PLATFORM,
            },
            "bindingList": {"deviceTypes": _SCALE_TYPES},
        }
        result = await self._post(EP_LOGIN, _encrypt(payload), auth=False)
        if not self._ok(result) or not result.get("data"):
            raise RenphoAuthError(
                f"login rejected: code={result.get('code')} message={_message(result)}", result.get("code")
            )
        login = _decrypt(result["data"]).get("login", {})
        self.token = login.get("token")
        self.user_id = str(login.get("id")) if login.get("id") is not None else None
        self.user_info = login
        if not self.token or not self.user_id:
            raise RenphoAuthError("login returned no token")

    async def _call(self, endpoint: str, body: Any) -> Any:
        result = await self._post(endpoint, _encrypt(body))
        if result.get("code") in (401, "401", 40001, "40001"):
            raise RenphoAuthError("token expired")
        if not self._ok(result):
            raise RenphoCloudError(f"{endpoint}: code={result.get('code')} message={_message(result)}")
        return _decrypt(result["data"]) if result.get("data") else None

    async def family(self) -> dict[str, str]:
        """user_id -> nickname; empty if the endpoint is unavailable."""
        try:
            data = await self._call(EP_FAMILY, {})
        except RenphoCloudError:
            _LOGGER.debug("family endpoint unavailable", exc_info=True)
            return {}
        members = data if isinstance(data, list) else (data or {}).get("list", [])
        out: dict[str, str] = {}
        for m in members or []:
            uid = str(m.get("userId", m.get("id", "")))
            if uid:
                out[uid] = _display_name(m) or uid
                self.family_records[uid] = m
        return out

    async def _pages(self, endpoint: str, table: str, user_id: str, *, everything: bool) -> list[dict]:
        """Fetch one person's records from one store.

        The sort order isn't documented (the 8-electrode store comes back
        newest-first, the classic one oldest-first), so unless ``everything``
        is asked for, paging stops as soon as the newest record must be in hand.
        """
        out: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            data = await self._call(
                endpoint,
                {"pageNum": page, "pageSize": _PAGE_SIZE, "userIds": [str(user_id)], "tableName": table},
            )
            recs = _records(data)
            if not recs:
                break
            out.extend(recs)
            if len(recs) < _PAGE_SIZE or (not everything and _ts(recs[0]) >= _ts(recs[-1])):
                break
        return out

    async def latest_measurements(self, *, everything: bool = False) -> dict[str, Any]:
        """Return ``{"users", "scales", "latest": {uid: record}, "records": [...]}``.

        Both record stores are read and merged: classic (4-electrode) records
        and body-composition (8-electrode / MorphoScan) records live apart.
        ``records`` holds every record fetched, oldest first — the newest pages
        only, unless ``everything`` (the full history, for the statistics import).
        """
        if not self.token:
            await self.login()
        try:
            devices = await self._call(EP_DEVICES, None) or {}
        except (RenphoAuthError, RenphoRateLimited):
            raise
        except RenphoCloudError:
            # Some accounts reject the empty-bytes body; renpho-api retries with {}
            devices = await self._call(EP_DEVICES, {}) or {}
        users = await self.family()
        users.setdefault(str(self.user_id), _display_name(self.user_info) or "Me")

        scales = devices.get("scale", []) if isinstance(devices, dict) else []
        targets: list[tuple[str, str]] = []
        for scale in scales:
            table = scale.get("tableName")
            if not table:
                continue
            uids = [str(u) for u in scale.get("userIds") or []] or [str(self.user_id)]
            targets.extend((table, uid) for uid in uids)
        # Family members not listed under a scale live in shard user_id % 24
        listed = {uid for _, uid in targets}
        for uid in users:
            if uid not in listed and uid.isdigit():
                targets.append((f"measurements_info_{int(uid) % _TABLE_SHARDS}", uid))

        merged: dict[tuple[str, int, Any], dict] = {}
        for table, queried in targets:
            for endpoint in (EP_MEASUREMENTS, EP_BODY_COMP):  # body comp last: it wins on overlap
                for r in await self._pages(endpoint, table, queried, everything=everything):
                    if r.get("invalidFlag") in (1, "1", True):
                        continue
                    # The record names its owner; the queried id is only a fallback
                    uid = str(r.get("bUserId") or r.get("userId") or queried)
                    key = (uid, _ts(r), r.get("weight"))
                    merged[key] = {**merged.get(key, {}), **r, "userId": uid}

        records = sorted(merged.values(), key=_ts)
        latest: dict[str, dict] = {}
        for r in records:
            latest[r["userId"]] = r  # oldest first, so the last one wins
            users.setdefault(r["userId"], r["userId"])
        people = {str(self.user_id): self.user_info, **self.family_records}
        profiles = {
            uid: profile_goals(people.get(uid, {}), _num(record.get("weight")))
            for uid, record in latest.items()
        }
        return {"users": users, "scales": scales, "latest": latest, "records": records, "profiles": profiles}
