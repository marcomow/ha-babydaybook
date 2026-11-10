"""An in-memory stand-in for the Firebase endpoints the integration calls."""

from __future__ import annotations

from typing import Any

from custom_components.babydaybook.api import (
    DOCUMENT_PREFIX,
    FIRESTORE_URL,
    IDENTITY_URL,
    TOKEN_URL,
    decode_fields,
    encode_fields,
)

USER_UID = "user-1"
SVT_START = 1_800_000_000


class _Response:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type: str | None = None) -> Any:
        return self._body

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeCloud:
    """Implements just enough of Firebase Auth and Firestore REST."""

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.refresh_token = "refresh-1"
        self.reject_delta_queries = False
        self._clock = SVT_START

    # ----- test helpers -----------------------------------------------------

    def put(self, path: str, data: dict[str, Any], *, stamp: bool = False) -> None:
        fields = encode_fields(data)
        if stamp:
            fields["svt"] = self._next_svt()
        self.documents[path] = fields

    def get(self, path: str) -> dict[str, Any]:
        return decode_fields(self.documents[path])

    def add_baby(self, uid: str, name: str) -> None:
        self.put(f"userData/{USER_UID}/createdBabies/{uid}", {"babyUid": uid})
        self.put(f"babyData/babyUid_{uid}", {"uid": uid, "name": name})

    def actions(self, baby_uid: str) -> list[dict[str, Any]]:
        prefix = f"babyData/babyUid_{baby_uid}/dailyActions/"
        return [self.get(path) for path in self.documents if path.startswith(prefix)]

    def count(self, fragment: str) -> int:
        return sum(1 for _, url, _ in self.requests if fragment in url)

    def _next_svt(self) -> dict[str, str]:
        self._clock += 1
        return {"timestampValue": f"2027-01-15T08:00:{self._clock - SVT_START:02d}.5Z"}

    def _wire(self, path: str) -> dict[str, Any]:
        return {"name": f"{DOCUMENT_PREFIX}/{path}", "fields": self.documents[path]}

    # ----- aiohttp.ClientSession stand-in ------------------------------------

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.requests.append((method, url, kwargs))
        return _Response(*self._handle(method, url, kwargs))

    def _handle(self, method: str, url: str, kwargs: dict[str, Any]) -> tuple[int, Any]:
        headers = kwargs.get("headers") or {}
        if url == TOKEN_URL:
            if "X-Android-Cert" not in headers:
                return 403, {"error": {"message": "Requests from this client are blocked."}}
            if kwargs["data"]["refresh_token"] != self.refresh_token:
                return 400, {"error": {"message": "INVALID_REFRESH_TOKEN"}}
            return 200, {
                "id_token": "id-token",
                "refresh_token": self.refresh_token,
                "user_id": USER_UID,
                "expires_in": "3600",
            }
        if url == f"{IDENTITY_URL}/accounts:signInWithPassword":
            if kwargs["json"]["password"] != "correct":
                return 400, {"error": {"message": "INVALID_LOGIN_CREDENTIALS"}}
            return 200, {
                "idToken": "id-token",
                "refreshToken": self.refresh_token,
                "localId": USER_UID,
                "expiresIn": "3600",
            }
        if url == f"{IDENTITY_URL}/accounts:sendOobCode":
            if kwargs["json"]["email"] != "parent@example.com":
                return 400, {"error": {"message": "EMAIL_NOT_FOUND"}}
            return 200, {"email": kwargs["json"]["email"]}
        if headers.get("Authorization") != "Bearer id-token":
            return 401, {"error": {"message": "Unauthenticated"}}
        if url == f"{FIRESTORE_URL}:commit":
            return self._commit(kwargs["json"]["writes"])
        if url.endswith(":runQuery"):
            parent = url.removeprefix(f"{FIRESTORE_URL}/").removesuffix(":runQuery")
            return self._query(parent, kwargs["json"]["structuredQuery"])
        path = url.removeprefix(f"{FIRESTORE_URL}/")
        if path in self.documents:
            return 200, self._wire(path)
        children = [
            self._wire(candidate)
            for candidate in self.documents
            if candidate.startswith(f"{path}/") and "/" not in candidate[len(path) + 1 :]
        ]
        if children or path.count("/") % 2 == 0:
            return 200, {"documents": children} if children else {}
        return 404, {"error": {"message": "Not found"}}

    def _commit(self, writes: list[dict[str, Any]]) -> tuple[int, Any]:
        for write in writes:
            path = write["update"]["name"].removeprefix(f"{DOCUMENT_PREFIX}/")
            exists = path in self.documents
            if write.get("currentDocument", {}).get("exists") is not None:
                if write["currentDocument"]["exists"] != exists:
                    return 409, {"error": {"message": "Precondition failed"}}
            fields = write["update"]["fields"]
            if "updateMask" in write:
                assert sorted(write["updateMask"]["fieldPaths"]) == sorted(fields)
                merged = {**self.documents.get(path, {}), **fields}
            else:
                merged = dict(fields)
            for transform in write.get("updateTransforms", []):
                assert transform == {"fieldPath": "svt", "setToServerValue": "REQUEST_TIME"}
                merged["svt"] = self._next_svt()
            self.documents[path] = merged
        return 200, {"writeResults": [{} for _ in writes]}

    def _query(self, parent: str, query: dict[str, Any]) -> tuple[int, Any]:
        collection = query["from"][0]["collectionId"]
        prefix = f"{parent}/{collection}/"
        paths = [path for path in self.documents if path.startswith(prefix)]
        order = query["orderBy"][0]
        field = order["field"]["fieldPath"]

        def key(path: str) -> Any:
            value = self.documents[path].get(field, {})
            if "integerValue" in value:
                return int(value["integerValue"])
            return value.get("timestampValue", "")

        if "where" in query:
            if self.reject_delta_queries:
                return 400, [{"error": {"message": "The query requires an index."}}]
            condition = query["where"]["fieldFilter"]
            assert condition["op"] == "GREATER_THAN"
            floor = condition["value"]["timestampValue"]
            paths = [path for path in paths if key(path) and key(path) > floor]
        else:
            paths = [path for path in paths if field in self.documents[path]]
        paths.sort(key=key, reverse=order["direction"] == "DESCENDING")
        rows = [{"document": self._wire(path)} for path in paths[: query["limit"]]]
        return 200, rows or [{"readTime": "2027-01-15T08:00:00Z"}]
