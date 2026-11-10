"""Async client for the Baby Daybook cloud backend.

Baby Daybook stores everything in Google Firebase. This module talks to the
same three public Google endpoints the Android app uses, and nothing else:

* ``identitytoolkit.googleapis.com`` - sign in with e-mail and password
* ``securetoken.googleapis.com``     - trade a refresh token for an ID token
* ``firestore.googleapis.com``       - read and write the records

It is a minimal Python port of the parts of the MIT-licensed
``kamilio/baby-daybook-sdk`` that are needed for diapers, sleep and
breastfeeding (see ``vendor/baby-daybook-sdk`` for the pinned reference
sources). It has no dependency on Home Assistant, only on ``aiohttp``.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp

# Public client identifiers of the Baby Daybook Android app (versionName 7.0.8).
# These are not secrets: they ship inside every copy of the APK. The API key is
# restricted to the Android app, so Google's auth endpoints only accept it
# together with the package name and signing-certificate fingerprint headers.
API_KEY = "AIzaSyDIjjUS-7888pKeaVgNM1g2lSLOX4i6Na8"
ANDROID_PACKAGE = "com.drillyapps.babydaybook"
ANDROID_CERT_SHA1 = "F63803E1E071269A0DDAB71664A1A55F6F27F8D4"
PROJECT_ID = "baby-daybook-app"

IDENTITY_URL = "https://identitytoolkit.googleapis.com/v1"
TOKEN_URL = "https://securetoken.googleapis.com/v1/token"
FIRESTORE_URL = (
    f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}"
    "/databases/(default)/documents"
)
DOCUMENT_PREFIX = f"projects/{PROJECT_ID}/databases/(default)/documents"

TYPE_DIAPER = "diaper_change"
TYPE_SLEEP = "sleeping"
TYPE_BREASTFEEDING = "breastfeeding"

SIDE_LEFT = "left"
SIDE_RIGHT = "right"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
_UID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# Firebase error codes that mean "the stored credentials are no longer valid".
_AUTH_ERROR_CODES = (
    "INVALID_REFRESH_TOKEN",
    "TOKEN_EXPIRED",
    "USER_DISABLED",
    "USER_NOT_FOUND",
    "INVALID_PASSWORD",
    "INVALID_LOGIN_CREDENTIALS",
    "EMAIL_NOT_FOUND",
    "INVALID_EMAIL",
    "MISSING_REFRESH_TOKEN",
    "INVALID_GRANT_TYPE",
)


class BabyDaybookError(Exception):
    """Base error for everything raised by this module."""


class BabyDaybookConnectionError(BabyDaybookError):
    """The backend could not be reached."""


class BabyDaybookAuthError(BabyDaybookError):
    """The credentials were rejected."""


class BabyDaybookApiError(BabyDaybookError):
    """The backend answered with an error."""

    def __init__(self, message: str, status: int | None = None) -> None:
        """Remember the HTTP status next to the message."""
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class Baby:
    """A baby the signed-in account has access to."""

    uid: str
    name: str


# --------------------------------------------------------------------------
# Firestore wire format
# --------------------------------------------------------------------------


def encode_value(value: Any) -> dict[str, Any]:
    """Encode a Python value as a Firestore REST value."""
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, dict):
        return {"mapValue": {"fields": encode_fields(value)}}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [encode_value(item) for item in value]}}
    raise TypeError(f"Unsupported Firestore value: {type(value).__name__}")


def encode_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Encode a dict as Firestore REST fields."""
    return {key: encode_value(value) for key, value in data.items()}


def decode_value(value: dict[str, Any]) -> Any:
    """Decode a Firestore REST value into a Python value."""
    if "stringValue" in value:
        return value["stringValue"]
    if "integerValue" in value:
        return int(value["integerValue"])
    if "booleanValue" in value:
        return value["booleanValue"]
    if "doubleValue" in value:
        return value["doubleValue"]
    if "timestampValue" in value:
        return value["timestampValue"]
    if "mapValue" in value:
        return decode_fields(value["mapValue"].get("fields", {}))
    if "arrayValue" in value:
        return [decode_value(item) for item in value["arrayValue"].get("values", [])]
    if "referenceValue" in value:
        return value["referenceValue"]
    return None


def decode_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Decode Firestore REST fields into a dict."""
    return {key: decode_value(value) for key, value in fields.items()}


def _decode_document(document: dict[str, Any]) -> dict[str, Any]:
    """Decode a Firestore document, keeping its id and raw sync timestamp."""
    fields = document.get("fields", {})
    data = decode_fields(fields)
    data["_id"] = document.get("name", "").rsplit("/", 1)[-1]
    # ``svt`` is the server timestamp the app's own sync is built on. Keep the
    # raw wire value so it can be sent back unchanged as a query cursor.
    data["_svt"] = fields.get("svt")
    data.setdefault("uid", data["_id"])
    return data


def svt_sort_key(raw: dict[str, Any] | None) -> float:
    """Turn a raw ``svt`` wire value into something comparable."""
    if not raw:
        return 0.0
    if "timestampValue" in raw:
        try:
            return datetime.fromisoformat(raw["timestampValue"]).timestamp()
        except ValueError:
            return 0.0
    if "integerValue" in raw:
        return int(raw["integerValue"]) / 1000
    return 0.0


def baby_document_id(baby_uid: str) -> str:
    """Return the Firestore document id of a baby."""
    return baby_uid if baby_uid.startswith("babyUid_") else f"babyUid_{baby_uid}"


def new_uid(length: int = 32) -> str:
    """Create a record id in the same shape the app generates."""
    return "".join(secrets.choice(_UID_ALPHABET) for _ in range(length))


def now_millis() -> int:
    """Return the current time in epoch milliseconds."""
    return int(time.time() * 1000)


# --------------------------------------------------------------------------
# Pure helpers over lists of activity records
# --------------------------------------------------------------------------


def is_deleted(action: dict[str, Any]) -> bool:
    """Return whether a record is a sync tombstone."""
    return bool(action.get("deleted"))


def running_action(
    actions: Iterable[dict[str, Any]], action_type: str
) -> dict[str, Any] | None:
    """Return the newest in-progress record of a type, if any."""
    candidates = [
        action
        for action in actions
        if action.get("type") == action_type
        and action.get("inProgress")
        and not is_deleted(action)
    ]
    return max(candidates, key=lambda a: a.get("startMillis", 0), default=None)


def last_action(
    actions: Iterable[dict[str, Any]], action_type: str
) -> dict[str, Any] | None:
    """Return the newest record of a type, if any."""
    candidates = [
        action
        for action in actions
        if action.get("type") == action_type and not is_deleted(action)
    ]
    return max(candidates, key=lambda a: a.get("startMillis", 0), default=None)


def build_action(
    *,
    user_uid: str,
    baby_uid: str,
    action_type: str,
    start_millis: int,
    in_progress: bool = False,
    side: str = "",
    pee: bool = False,
    poo: bool = False,
) -> dict[str, Any]:
    """Build a complete ``dailyActions`` record the way the app writes them."""
    return {
        "uid": new_uid(),
        "userUid": user_uid,
        "babyUid": baby_uid,
        "type": action_type,
        "startMillis": start_millis,
        "updatedMillis": now_millis(),
        "rev": 4,
        "groupUid": "",
        "notes": "",
        "inProgress": 1 if in_progress else 0,
        "endMillis": 0,
        "duration": 0,
        "pauseMillis": 0,
        "leftDuration": 0,
        "rightDuration": 0,
        "side": side,
        "reaction": "",
        "amount": 0,
        "amountUnit": "",
        "temperature": 0,
        "hairWash": 0,
        "volume": 0,
        "pee": 1 if pee else 0,
        "poo": 1 if poo else 0,
    }


def stop_changes(action: dict[str, Any], end_millis: int) -> dict[str, Any]:
    """Return the fields that change when a running or paused timer is stopped."""
    running = bool(action.get("inProgress"))
    paused_at = action.get("pauseMillis") or 0
    if not running and paused_at:
        end_millis = min(end_millis, paused_at)
    start = action.get("startMillis", end_millis)
    left = action.get("leftDuration") or 0
    right = action.get("rightDuration") or 0
    changes: dict[str, Any] = {
        "endMillis": end_millis,
        "duration": max(0, end_millis - start),
        "inProgress": 0,
        "pauseMillis": 0,
        "updatedMillis": now_millis(),
    }
    side = action.get("side")
    if action.get("type") == TYPE_BREASTFEEDING and side in (SIDE_LEFT, SIDE_RIGHT):
        elapsed = max(0, end_millis - start - left - right)
        changes["leftDuration"] = left + (elapsed if side == SIDE_LEFT else 0)
        changes["rightDuration"] = right + (elapsed if side == SIDE_RIGHT else 0)
    return changes


def switch_side_changes(
    action: dict[str, Any], side: str, at_millis: int
) -> dict[str, Any]:
    """Return the fields that change when breastfeeding moves to the other side."""
    left = action.get("leftDuration") or 0
    right = action.get("rightDuration") or 0
    elapsed = max(0, at_millis - action.get("startMillis", at_millis) - left - right)
    previous = action.get("side")
    return {
        "side": side,
        "leftDuration": left + (elapsed if previous == SIDE_LEFT else 0),
        "rightDuration": right + (elapsed if previous == SIDE_RIGHT else 0),
        "updatedMillis": now_millis(),
    }


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class BabyDaybookClient:
    """Talks to Baby Daybook's Firebase backend on behalf of one account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        refresh_token: str,
        *,
        on_refresh_token: Callable[[str], None] | None = None,
    ) -> None:
        """Create a client from a Firebase refresh token."""
        self._session = session
        self._refresh_token = refresh_token
        self._on_refresh_token = on_refresh_token
        self._id_token: str | None = None
        self._expires_at = 0.0
        self._refresh_lock = asyncio.Lock()
        self.user_uid: str = ""

    @property
    def refresh_token(self) -> str:
        """Return the current refresh token."""
        return self._refresh_token

    # ----- authentication -------------------------------------------------

    @staticmethod
    def _android_headers() -> dict[str, str]:
        return {
            "X-Android-Package": ANDROID_PACKAGE,
            "X-Android-Cert": ANDROID_CERT_SHA1,
        }

    @classmethod
    async def sign_in_with_password(
        cls, session: aiohttp.ClientSession, email: str, password: str
    ) -> BabyDaybookClient:
        """Sign in with e-mail and password and return a ready client."""
        body = await _request(
            session,
            "POST",
            f"{IDENTITY_URL}/accounts:signInWithPassword",
            params={"key": API_KEY},
            headers=cls._android_headers(),
            json={"email": email, "password": password, "returnSecureToken": True},
            auth_endpoint=True,
        )
        client = cls(session, body["refreshToken"])
        client._id_token = body["idToken"]
        client._expires_at = time.monotonic() + float(body.get("expiresIn", 3600))
        client.user_uid = body["localId"]
        return client

    @classmethod
    async def send_password_email(
        cls, session: aiohttp.ClientSession, email: str
    ) -> None:
        """Ask Baby Daybook to e-mail a link for setting an account password.

        For an account that only ever used Google sign-in, following the link
        adds a password to that same account; Google sign-in keeps working.
        """
        await _request(
            session,
            "POST",
            f"{IDENTITY_URL}/accounts:sendOobCode",
            params={"key": API_KEY},
            headers=cls._android_headers(),
            json={"requestType": "PASSWORD_RESET", "email": email},
            auth_endpoint=True,
        )

    async def authenticate(self) -> str:
        """Force a token refresh and return the account's user id."""
        await self._get_id_token(force=True)
        return self.user_uid

    async def _get_id_token(self, *, force: bool = False) -> str:
        async with self._refresh_lock:
            if (
                not force
                and self._id_token
                and self._expires_at - time.monotonic() > 60
            ):
                return self._id_token
            body = await _request(
                self._session,
                "POST",
                TOKEN_URL,
                params={"key": API_KEY},
                headers=self._android_headers(),
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
                auth_endpoint=True,
            )
            self._id_token = body["id_token"]
            self._expires_at = time.monotonic() + float(body.get("expires_in", 3600))
            self.user_uid = body["user_id"]
            new_refresh_token = body.get("refresh_token")
            if new_refresh_token and new_refresh_token != self._refresh_token:
                self._refresh_token = new_refresh_token
                if self._on_refresh_token:
                    self._on_refresh_token(new_refresh_token)
            return self._id_token

    # ----- Firestore primitives --------------------------------------------

    async def _firestore(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json: Any = None,
    ) -> Any:
        for attempt in range(2):
            token = await self._get_id_token(force=attempt > 0)
            try:
                return await _request(
                    self._session,
                    method,
                    url,
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except BabyDaybookApiError as err:
                # A 401 means the ID token was rejected: refresh once and retry.
                if err.status != 401 or attempt > 0:
                    raise
        raise AssertionError("unreachable")

    async def get_document(self, path: str) -> dict[str, Any] | None:
        """Read one document, or None if it does not exist."""
        try:
            document = await self._firestore("GET", f"{FIRESTORE_URL}/{path}")
        except BabyDaybookApiError as err:
            if err.status == 404:
                return None
            raise
        return _decode_document(document)

    async def list_documents(self, collection_path: str) -> list[dict[str, Any]]:
        """Read every document of a (small) collection."""
        documents: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params = {"pageSize": "300"}
            if page_token:
                params["pageToken"] = page_token
            page = await self._firestore(
                "GET", f"{FIRESTORE_URL}/{collection_path}", params=params
            )
            documents.extend(_decode_document(doc) for doc in page.get("documents", []))
            page_token = page.get("nextPageToken")
            if not page_token:
                return documents

    async def _query_actions(
        self, baby_uid: str, query: dict[str, Any]
    ) -> list[dict[str, Any]]:
        rows = await self._firestore(
            "POST",
            f"{FIRESTORE_URL}/babyData/{baby_document_id(baby_uid)}:runQuery",
            json={"structuredQuery": {"from": [{"collectionId": "dailyActions"}], **query}},
        )
        return [_decode_document(row["document"]) for row in rows if "document" in row]

    async def _write(
        self, path: str, data: dict[str, Any], *, merge: bool
    ) -> None:
        """Write a document and stamp it with the server time, like the app."""
        write: dict[str, Any] = {
            "update": {
                "name": f"{DOCUMENT_PREFIX}/{path}",
                "fields": encode_fields(data),
            },
            "updateTransforms": [
                {"fieldPath": "svt", "setToServerValue": "REQUEST_TIME"}
            ],
        }
        if merge:
            write["updateMask"] = {"fieldPaths": list(data)}
            write["currentDocument"] = {"exists": True}
        else:
            write["currentDocument"] = {"exists": False}
        await self._firestore("POST", f"{FIRESTORE_URL}:commit", json={"writes": [write]})

    # ----- reads -------------------------------------------------------------

    async def list_babies(self) -> list[Baby]:
        """Return the babies this account created or was invited to."""
        if not self.user_uid:
            await self.authenticate()
        created, accepted = await asyncio.gather(
            self.list_documents(f"userData/{self.user_uid}/createdBabies"),
            self.list_documents(f"userData/{self.user_uid}/acceptedInvites"),
        )
        uids: list[str] = []
        for link in (*created, *accepted):
            if is_deleted(link):
                continue
            uid = str(link.get("babyUid") or link["_id"]).removeprefix("babyUid_")
            if uid and uid not in uids:
                uids.append(uid)
        documents = await asyncio.gather(
            *(self.get_document(f"babyData/{baby_document_id(uid)}") for uid in uids)
        )
        return [
            Baby(uid=uid, name=str(document.get("name") or uid))
            for uid, document in zip(uids, documents, strict=True)
            if document and not is_deleted(document)
        ]

    async def recent_actions(self, baby_uid: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return the newest activity records of a baby."""
        return await self._query_actions(
            baby_uid,
            {
                "orderBy": [
                    {"field": {"fieldPath": "startMillis"}, "direction": "DESCENDING"}
                ],
                "limit": limit,
            },
        )

    async def changed_actions(
        self, baby_uid: str, since_svt: dict[str, Any], limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return records written after a sync timestamp (usually none)."""
        return await self._query_actions(
            baby_uid,
            {
                "where": {
                    "fieldFilter": {
                        "field": {"fieldPath": "svt"},
                        "op": "GREATER_THAN",
                        "value": since_svt,
                    }
                },
                "orderBy": [{"field": {"fieldPath": "svt"}, "direction": "ASCENDING"}],
                "limit": limit,
            },
        )

    # ----- writes ------------------------------------------------------------

    def _action_path(self, baby_uid: str, uid: str) -> str:
        return f"babyData/{baby_document_id(baby_uid)}/dailyActions/{uid}"

    async def _create_action(self, action: dict[str, Any]) -> dict[str, Any]:
        await self._write(
            self._action_path(action["babyUid"], action["uid"]), action, merge=False
        )
        return action

    async def _update_action(
        self, action: dict[str, Any], changes: dict[str, Any]
    ) -> dict[str, Any]:
        await self._write(
            self._action_path(action["babyUid"], action["uid"]), changes, merge=True
        )
        return {**action, **changes}

    async def log_diaper(
        self, baby_uid: str, *, pee: bool, poo: bool, at_millis: int | None = None
    ) -> dict[str, Any]:
        """Record a diaper change."""
        if not self.user_uid:
            await self.authenticate()
        return await self._create_action(
            build_action(
                user_uid=self.user_uid,
                baby_uid=baby_uid,
                action_type=TYPE_DIAPER,
                start_millis=at_millis or now_millis(),
                pee=pee,
                poo=poo,
            )
        )

    async def start_timer(
        self, baby_uid: str, action_type: str, *, side: str = ""
    ) -> dict[str, Any]:
        """Start a sleep or breastfeeding timer."""
        if not self.user_uid:
            await self.authenticate()
        return await self._create_action(
            build_action(
                user_uid=self.user_uid,
                baby_uid=baby_uid,
                action_type=action_type,
                start_millis=now_millis(),
                in_progress=True,
                side=side,
            )
        )

    async def stop_timer(self, action: dict[str, Any]) -> dict[str, Any]:
        """Stop a running timer."""
        return await self._update_action(action, stop_changes(action, now_millis()))

    async def switch_side(self, action: dict[str, Any], side: str) -> dict[str, Any]:
        """Move a running breastfeeding timer to the other side."""
        return await self._update_action(
            action, switch_side_changes(action, side, now_millis())
        )


async def _request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    auth_endpoint: bool = False,
    **kwargs: Any,
) -> Any:
    """Send one request and translate failures into this module's errors."""
    try:
        async with session.request(
            method, url, timeout=REQUEST_TIMEOUT, **kwargs
        ) as response:
            status = response.status
            try:
                body = await response.json(content_type=None)
            except ValueError:
                body = None
    except (aiohttp.ClientError, TimeoutError) as err:
        raise BabyDaybookConnectionError(f"Could not reach {url.split('?')[0]}") from err

    if status < 400:
        return body

    # runQuery streams a list; errors arrive as a one-element list.
    if isinstance(body, list) and body:
        body = body[0]
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    message = str(message or error or f"HTTP {status}")
    if auth_endpoint and (
        status in (401, 403) or message.startswith(_AUTH_ERROR_CODES)
    ):
        raise BabyDaybookAuthError(message)
    raise BabyDaybookApiError(message, status)
