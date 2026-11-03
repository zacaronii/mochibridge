"""Async client for the kechimochi web build's /api (verified against web-0.3.3)."""

from __future__ import annotations

from typing import Any, Callable

import httpx

MEDIA_FIELDS = ("id", "uid", "title", "variant", "default_activity_type", "status", "language",
                "description", "cover_image", "extra_data", "content_type", "tracking_status")
LOG_FIELDS = ("id", "media_id", "duration_minutes", "characters", "date", "date_precision",
              "activity_type", "notes")


class KechimochiError(Exception):
    def __init__(self, status: int | None, message: str):
        super().__init__(f"{status}: {message}" if status else message)
        self.status = status
        self.message = message

    @property
    def transient(self) -> bool:
        """Worth retrying: connection problems and server errors."""
        return self.status is None or self.status >= 500 or self.status == 429

    @property
    def not_found(self) -> bool:
        """PUT on a missing log answers 400 "Activity N not found" in web-0.3.3, not 404."""
        return self.status == 404 or (self.status == 400 and "not found" in self.message.lower())


class Conflict(KechimochiError):
    pass


class ReadOnlyViolation(RuntimeError):
    """Raised (and deliberately not a KechimochiError, so nothing retries it) when a read-only client
    is asked to write."""


def media_payload(media: dict[str, Any]) -> dict[str, Any]:
    out = {k: media.get(k) for k in MEDIA_FIELDS}
    if out.get("uid") is None:
        out.pop("uid")
    return out


def log_payload(log: dict[str, Any]) -> dict[str, Any]:
    """GET /api/logs adds joined title/language fields; PUT/POST only take LOG_FIELDS."""
    return {k: log.get(k) for k in LOG_FIELDS}


class Kechimochi:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=5),
            headers={"X-Kechimochi-API": "1", "User-Agent": "mochibridge"},
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = await self._client.request(method, f"{self.base_url}/api{path}", **kw)
        except httpx.HTTPError as e:
            raise KechimochiError(None, f"{method} {path}: {e!r}") from e
        if r.status_code == 409:
            raise Conflict(409, r.text)
        if r.status_code >= 400:
            raise KechimochiError(r.status_code, f"{method} {path}: {r.text[:300]}")
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.content

    # --- health ----------------------------------------------------------
    async def version(self) -> str:
        return await self._request("GET", "/version")

    # --- media -----------------------------------------------------------
    async def list_media(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/media")

    async def get_media(self, media_id: int) -> dict[str, Any] | None:
        return next((m for m in await self.list_media() if m["id"] == media_id), None)

    async def find_media(self, title: str, variant: str = "") -> dict[str, Any] | None:
        variant = variant.strip()
        return next((m for m in await self.list_media()
                     if m["title"] == title and (m.get("variant") or "").strip() == variant), None)

    async def create_media(self, media: dict[str, Any]) -> int:
        body = media_payload({**media, "id": None, "uid": None})
        return int(await self._request("POST", "/media", json=body))

    async def put_media(self, media: dict[str, Any]) -> None:
        await self._request("PUT", f"/media/{media['id']}", json=media_payload(media))

    async def update_media(self, media_id: int,
                           change: Callable[[dict[str, Any]], dict[str, Any] | None]) -> dict[str, Any] | None:
        """GET the current entry, let `change` edit a copy, PUT it back only if something changed.

        PUT is a full replace, so we always start from the live object."""
        current = await self.get_media(media_id)
        if current is None:
            raise KechimochiError(404, f"media {media_id} not found")
        updated = change(dict(current))
        if updated is None or media_payload(updated) == media_payload(current):
            return None
        await self.put_media(updated)
        return updated

    async def delete_media(self, media_id: int) -> None:
        await self._request("DELETE", f"/media/{media_id}")

    # --- logs ------------------------------------------------------------
    async def list_logs(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/logs")

    async def logs_for_media(self, media_id: int) -> list[dict[str, Any]]:
        return await self._request("GET", f"/logs/media/{media_id}")

    async def create_log(self, log: dict[str, Any]) -> int:
        return int(await self._request("POST", "/logs", json=log_payload({**log, "id": None})))

    async def put_log(self, log_id: int, log: dict[str, Any]) -> None:
        await self._request("PUT", f"/logs/{log_id}", json=log_payload({**log, "id": log_id}))

    async def delete_log(self, log_id: int) -> None:
        await self._request("DELETE", f"/logs/{log_id}")

    async def milestones(self, media_uid: str) -> list[dict[str, Any]]:
        return await self._request("GET", f"/media/{media_uid}/milestones") or []

    async def export_full_backup(self) -> bytes:
        """kechimochi's own full backup (ZIP of DBs and covers), restorable via its import."""
        data = await self._request("POST", "/export/full-backup", json={"local_storage": "{}", "version": "kbridge"})
        if not isinstance(data, (bytes, bytearray)) or not bytes(data).startswith(b"PK"):
            raise KechimochiError(None, "full backup did not return a ZIP")
        return bytes(data)

    async def import_full_backup(self, data: bytes) -> Any:
        return await self._request("POST", "/import/full-backup",
                                   files={"file": ("kechimochi_full_backup.zip", data, "application/zip")})

    # --- covers ----------------------------------------------------------
    async def upload_cover(self, media_id: int, data: bytes, filename: str = "cover.jpg",
                           content_type: str = "image/jpeg") -> str:
        res = await self._request("POST", f"/covers/{media_id}",
                                  files={"file": (filename, data, content_type)})
        return res["path"]


class ReadOnlyKechimochi(Kechimochi):
    """Used by previews: any non-GET request is a bug and stops the run."""

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        if method.upper() != "GET":
            raise ReadOnlyViolation(f"read-only client refused {method} {path}")
        return await super()._request(method, path, **kw)
