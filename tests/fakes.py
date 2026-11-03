"""In-memory stand-in for the kechimochi API, matching the behaviour verified against kechimochi web-0.3.3."""
import copy
import json

from kbridge.kechimochi import Conflict, KechimochiError, log_payload, media_payload


class FakeKechimochi:
    def __init__(self, media=None):
        self.media = {m["id"]: copy.deepcopy(m) for m in (media or [])}
        self.logs = {}
        self.calls = []
        self._next = max(self.media, default=0) + 1

    def _id(self):
        self._next += 1
        return self._next - 1

    async def version(self):
        return "web-fake"

    async def list_media(self):
        self.calls.append(("GET", "/media"))
        return [copy.deepcopy(m) for m in self.media.values()]

    async def get_media(self, media_id):
        m = self.media.get(media_id)
        return copy.deepcopy(m) if m else None

    async def find_media(self, title, variant=""):
        return next((copy.deepcopy(m) for m in self.media.values()
                     if m["title"] == title and (m.get("variant") or "") == variant), None)

    async def create_media(self, media):
        if await self.find_media(media["title"], media.get("variant", "")):
            raise Conflict(409, "exists")
        mid = self._id()
        self.media[mid] = {**media_payload(media), "id": mid, "uid": f"uid-{mid}"}
        self.calls.append(("POST", "/media", mid))
        return mid

    async def put_media(self, media):
        assert set(media_payload(media)) >= {"title", "status", "extra_data"}
        self.media[media["id"]] = media_payload(media) | {"uid": self.media[media["id"]]["uid"]}
        self.calls.append(("PUT", f"/media/{media['id']}"))

    async def list_logs(self):
        out = []
        for l in self.logs.values():
            m = self.media.get(l["media_id"], {})
            out.append({**l, "title": m.get("title"), "language": m.get("language")})
        return out

    async def create_log(self, log):
        if not log["duration_minutes"] and not log["characters"]:
            raise KechimochiError(400, "Activity must have either duration or characters")
        lid = self._id()
        self.logs[lid] = {**log_payload(log), "id": lid}
        self.calls.append(("POST", "/logs", lid))
        return lid

    async def put_log(self, log_id, log):
        if log_id not in self.logs:
            raise KechimochiError(400, f"PUT /logs/{log_id}: Activity {log_id} not found")
        self.logs[log_id] = {**log_payload(log), "id": log_id}
        self.calls.append(("PUT", f"/logs/{log_id}"))

    async def delete_log(self, log_id):
        if log_id not in self.logs:
            raise KechimochiError(400, f"Activity {log_id} not found")
        del self.logs[log_id]
        self.calls.append(("DELETE", f"/logs/{log_id}"))

    async def logs_for_media(self, media_id):
        return [l for l in await self.list_logs() if l["media_id"] == media_id]

    async def delete_media(self, media_id):
        assert not [l for l in self.logs.values() if l["media_id"] == media_id], "kechimochi would cascade logs"
        del self.media[media_id]
        self.calls.append(("DELETE", f"/media/{media_id}"))

    milestone_uids: set = set()

    async def milestones(self, uid):
        return [{"id": 1}] if uid in self.milestone_uids else []

    async def upload_cover(self, media_id, data, filename="cover.jpg", content_type="image/jpeg"):
        path = f"/data/covers/{media_id}_x.jpg"
        self.media[media_id]["cover_image"] = path
        self.calls.append(("POST", f"/covers/{media_id}"))
        return path

    async def aclose(self):
        pass


def entry(mid, title, variant="", extra=None, **kw):
    return {"id": mid, "uid": f"uid-{mid}", "title": title, "variant": variant, "default_activity_type": "Listening",
            "status": "Active", "language": "Japanese", "description": kw.get("description", ""),
            "cover_image": kw.get("cover_image", ""), "extra_data": json.dumps(extra or {}, ensure_ascii=False),
            "content_type": kw.get("content_type", "Audio"), "tracking_status": "Ongoing"}
