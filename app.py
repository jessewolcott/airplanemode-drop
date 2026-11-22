"""Drop: pass files and text between every device on the AirplaneMode hotspot.

API (all relative to the app's root; the platform serves it at /drop/):
  GET    api/items                 every item (flat, newest first, with "parent"), storage use, admin flag
  PUT    api/files?name=&parent=   upload one file into a folder (or the top); the body is the file (streamed)
  POST   api/texts   {text, parent}   share a text snippet or link
  POST   api/folders {name, parent}   make a folder
  GET    api/files/{id}            stream or download (HTTP Range supported, so media can seek)
  PATCH  api/items/{id} {name?, parent?, nodownload?}   rename, move, or (admin) turn downloads off
  POST   api/items/{id}/pin {pinned}   keep an item past the expiry (on a folder: everything inside)
  DELETE api/items/{id}            (a folder goes with everything in it)

Changes need the header "X-Drop: 1". Browsers can't send it cross-site without a CORS
preflight, which this app never allows, so other websites can't add or delete items.
There's no login: anyone on the hotspot can use Drop. The hotspot password is the access control.

Admin: whoever is signed in to the AirplaneMode admin panel. Drop is served from the same
address, so it gets the panel's session cookie and asks the admin service (ADMIN_URL) whether
it's valid. Only the admin can turn downloads off ("nodownload") for a file or a folder; that
hides "save" and refuses ?dl=1 for everyone else, while streaming (to the page or a player app)
keeps working. It keeps big files on the box; it can't stop someone recording the stream.
"""
import asyncio
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import time
import unicodedata
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.requests import ClientDisconnect

log = logging.getLogger("drop")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def parse_size(s, default):
    s = (s or "").strip().lower().rstrip("b") or default
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgt]?)", s)
    if not m:
        raise ValueError(f"bad size: {s!r}")
    return int(float(m.group(1)) * 1024 ** "_kmgt".index(m.group(2) or "_"))


HERE = Path(__file__).parent
DATA = Path(os.getenv("DATA_DIR", "/data"))
MAX_BYTES = parse_size(os.getenv("DROP_MAX_BYTES"), "4g")
MAX_FILE = parse_size(os.getenv("DROP_MAX_FILE"), "2g")
KEEP_DAYS = float(os.getenv("DROP_KEEP_DAYS", "7"))
MAX_TEXT = 64 * 1024
DISK_MARGIN = 64 * 1024 ** 2         # always leave this much of the disk free
CHUNK = 1024 ** 2                    # write to disk in 1 MB pieces
ID_RE = re.compile(r"^[0-9a-f]{16}$")
MAX_DEPTH = 8                        # folders inside folders

# The platform's admin service. Empty (standalone): nobody is admin.
ADMIN_URL = os.getenv("ADMIN_URL", "").rstrip("/")
ADMIN_COOKIE = "am_admin"
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

FILES, TMP, META = DATA / "files", DATA / "tmp", DATA / "items.json"

# Shown in the page (inline). Everything else downloads as an attachment, so an uploaded
# HTML or SVG file can never run as a page on the box's own address.
INLINE_TYPES = re.compile(r"^(image/(png|jpeg|gif|webp|avif|heic|heif|bmp)|audio/.+|video/.+|text/plain)$")


def now():
    return time.time()


# For files recovered without their metadata: guess the type from the first bytes.
MAGIC = [(4, b"ftyp", "video/mp4", ".mp4"), (0, b"\x1aE\xdf\xa3", "video/webm", ".webm"), (0, b"ID3", "audio/mpeg", ".mp3"),
         (0, b"\xff\xfb", "audio/mpeg", ".mp3"), (0, b"\xff\xd8\xff", "image/jpeg", ".jpg"), (0, b"\x89PNG", "image/png", ".png"),
         (0, b"GIF8", "image/gif", ".gif"), (0, b"%PDF", "application/pdf", ".pdf"), (0, b"OggS", "audio/ogg", ".ogg")]


def sniff(path):
    try:
        head = path.open("rb").read(16)
    except OSError:
        return "application/octet-stream", ""
    for off, sig, kind, ext in MAGIC:
        if head[off:off + len(sig)] == sig:
            return kind, ext
    return "application/octet-stream", ""


# ---------------- items (metadata in one JSON file) ----------------
class Store:
    def __init__(self):
        self.items = []
        self.version = 0
        self.reserved = {}           # upload id -> bytes it's holding
        self.lock = asyncio.Lock()

    def load(self):
        for d in (FILES, TMP):
            d.mkdir(parents=True, exist_ok=True)
        for p in TMP.iterdir():      # partial uploads from before a restart
            p.unlink(missing_ok=True)
        # With no readable items.json, the files on disk are all there is: keep them (as
        # "recovered" items) rather than treating them as leftovers. Only a good items.json
        # lets us delete files it doesn't know about.
        recovering = False
        try:
            self.items = json.loads(META.read_text(encoding="utf-8"))["items"]
        except FileNotFoundError:
            self.items = []
            recovering = True
        except Exception as e:
            bad = META.with_name(f"items.json.bad-{int(now())}")
            log.warning("could not read %s (%s); kept it as %s and recovering files from disk", META, e, bad.name)
            META.replace(bad)
            self.items = []
            recovering = True
        # drop entries whose file is gone, and files nobody knows about
        known = {i["id"] for i in self.items}
        self.items = [i for i in self.items if i["kind"] != "file" or (FILES / i["id"]).exists()]
        recovered = 0
        for p in FILES.iterdir():
            if p.name not in known and recovering and ID_RE.match(p.name):
                kind, ext = sniff(p)
                st = p.stat()
                self.items.append({"id": p.name, "kind": "file", "name": f"recovered-{p.name[:6]}{ext}", "size": st.st_size,
                                   "type": kind, "created": st.st_mtime, "pinned": True, "nodownload": False, "parent": None})
                known.add(p.name)
                recovered += 1
        if recovered:
            log.warning("recovered %d file(s) without metadata; they're kept until someone deletes them", recovered)
        # items from 0.1 have no parent/nodownload; parents that vanished mean "the top"
        folders = {i["id"] for i in self.items if i["kind"] == "folder"}
        for i in self.items:
            i.setdefault("nodownload", False)
            if i.get("parent") not in folders:
                i["parent"] = None
        for p in FILES.iterdir():
            if p.name not in known:
                p.unlink(missing_ok=True)
        self.version = 1
        if recovered:
            self.save()

    def save(self):
        tmp = META.with_suffix(".tmp")
        tmp.write_text(json.dumps({"items": self.items}, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(META)
        self.version += 1

    def used(self):
        return sum(i["size"] for i in self.items if i["kind"] == "file")

    def held(self):
        return self.used() + sum(self.reserved.values())

    def free(self):
        disk = shutil.disk_usage(DATA).free - DISK_MARGIN
        return max(0, min(MAX_BYTES - self.held(), disk))

    def by_id(self):
        return {i["id"]: i for i in self.items}

    def chain(self, item, ids=None):
        """The item, then its folder, that folder's folder... up to the top."""
        ids = ids or self.by_id()
        seen = set()
        while item and item["id"] not in seen:
            seen.add(item["id"])
            yield item
            item = ids.get(item.get("parent"))

    def kept(self, item, ids=None):
        return any(i.get("pinned") for i in self.chain(item, ids))

    def blocked(self, item, ids=None):
        """The item or folder that turned downloads off for this item, or None."""
        return next((i for i in self.chain(item, ids) if i.get("nodownload")), None)

    def descendants(self, folder_id):
        out, todo = [], [folder_id]
        while todo:
            fid = todo.pop()
            kids = [i for i in self.items if i.get("parent") == fid]
            out += kids
            todo += [k["id"] for k in kids if k["kind"] == "folder"]
        return out

    def depth(self, fid):
        """How many folders deep a folder is (the top = 0)."""
        return sum(1 for _ in self.chain(self.by_id().get(fid))) if fid else 0

    def folder(self, fid):
        """A folder id from the client -> a folder that exists (None = the top)."""
        if fid in (None, "", "root"):
            return None
        item = self.get(fid)
        if item["kind"] != "folder":
            raise HTTPException(400, "That's not a folder.")
        return fid

    def get(self, iid):
        if not ID_RE.match(iid):
            raise HTTPException(404, "No such item.")
        item = next((i for i in self.items if i["id"] == iid), None)
        if not item:
            raise HTTPException(404, "No such item.")
        return item

    def sweep(self):
        """Remove expired, unpinned items."""
        cutoff = now() - KEEP_DAYS * 86400
        ids = self.by_id()
        # folders themselves never expire (they're tiny); "keep" on a folder keeps what's inside
        old = [i for i in self.items if i["kind"] != "folder" and i["created"] < cutoff and not self.kept(i, ids)]
        if not old:
            return 0
        for i in old:
            if i["kind"] == "file":
                (FILES / i["id"]).unlink(missing_ok=True)
        gone = {i["id"] for i in old}
        self.items = [i for i in self.items if i["id"] not in gone]
        self.save()
        log.info("expired %d item(s)", len(old))
        return len(old)


store = Store()


async def _sweep_forever():
    while True:
        await asyncio.sleep(600)
        try:
            async with store.lock:
                store.sweep()
        except Exception as e:
            log.warning("sweep failed: %s", e)


@asynccontextmanager
async def lifespan(_app):
    store.load()
    store.sweep()
    task = asyncio.create_task(_sweep_forever())
    yield
    task.cancel()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def need_header(request: Request):
    if request.headers.get("x-drop") != "1":
        raise HTTPException(403, "Missing request header.")


def _ask_admin_service(token):
    req = urllib.request.Request(f"{ADMIN_URL}/api/admin/session", headers={"Cookie": f"{ADMIN_COOKIE}={token}"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


check_admin_token = _ask_admin_service   # tests replace this
_admin_cache = {}                         # token -> (is admin, checked until)


async def is_admin(request: Request):
    token = request.cookies.get(ADMIN_COOKIE, "")
    if not ADMIN_URL or not TOKEN_RE.match(token):
        return False
    hit = _admin_cache.get(token)
    if hit and hit[1] > now():
        return hit[0]
    answer = await anyio.to_thread.run_sync(check_admin_token, token)
    if len(_admin_cache) > 256:
        _admin_cache.clear()
    _admin_cache[token] = (answer, now() + 30)
    return answer


def clean_name(name):
    """A display name only (files are stored by id): last path part, no control characters."""
    name = unicodedata.normalize("NFC", name or "")
    name = re.split(r"[\\/]", name)[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C").strip().strip(".")
    return name[:200] or "file"


def human(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


# ---------------- reading ----------------
@app.get("/api/items")
async def items(request: Request):
    async with store.lock:
        store.sweep()
    ids = store.by_id()
    out = []
    for i in sorted(store.items, key=lambda i: -i["created"]):
        b = store.blocked(i, ids)
        out.append({**i, "kept": store.kept(i, ids), "download": b is None,
                    "blocked_by": None if b is None or b is i else b["id"]})
    return {"version": store.version, "items": out,
            "admin": await is_admin(request), "admin_available": bool(ADMIN_URL),
            "used": store.used(), "cap": MAX_BYTES, "free": store.free(), "max_file": MAX_FILE,
            "keep_days": KEEP_DAYS}


def content_disposition(kind, name):
    ascii_name = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\;' else "_" for ch in name)
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


def parse_range(header, size):
    """One 'bytes=' range -> (start, end) inclusive, None for the whole file, or 'bad'."""
    m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", header or "")
    if not m or (not m.group(1) and not m.group(2)):
        return None if not header or "," in header else "bad"
    if m.group(1):
        start = int(m.group(1))
        end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
    else:  # suffix: the last N bytes
        n = int(m.group(2))
        if n == 0:
            return "bad"
        start, end = max(0, size - n), size - 1
    if start >= size or start > end:
        return "bad"
    return start, end


@app.api_route("/api/files/{iid}", methods=["GET", "HEAD"])
async def download(iid: str, request: Request, dl: bool = False):
    item = store.get(iid)
    if item["kind"] != "file":
        raise HTTPException(404, "No such file.")
    path = FILES / iid
    size = path.stat().st_size
    ctype = item["type"]
    inline = not dl and INLINE_TYPES.match(ctype)
    if not inline and store.blocked(item) and not await is_admin(request):
        # streaming to the page or a player still works; saving a copy doesn't
        raise HTTPException(403, "Downloads are turned off for this. You can still play it here.")
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": content_disposition("inline" if inline else "attachment", item["name"]),
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; media-src 'self'",
        "Cache-Control": "private, max-age=3600",
    }
    if not inline:
        ctype = "application/octet-stream"
    rng = parse_range(request.headers.get("range"), size)
    if rng == "bad":
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}", **headers})
    start, end = rng or (0, size - 1)
    length = end - start + 1 if size else 0
    status = 206 if rng else 200
    if rng:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(length)
    if request.method == "HEAD":
        return Response(status_code=status, headers=headers, media_type=ctype)

    async def body():
        async with await anyio.open_file(path, "rb") as f:
            await f.seek(start)
            left = length
            while left > 0:
                chunk = await f.read(min(256 * 1024, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    return StreamingResponse(body(), status_code=status, headers=headers, media_type=ctype)


# ---------------- changes ----------------
@app.put("/api/files")
async def upload(request: Request, name: str = "file", parent: str | None = None):
    need_header(request)
    name = clean_name(name)
    store.folder(parent)  # fail before receiving anything
    declared = request.headers.get("content-length")
    declared = int(declared) if declared and declared.isdigit() else None
    if declared is not None:
        if declared > MAX_FILE:
            raise HTTPException(413, f"{name} is {human(declared)}; the limit per file is {human(MAX_FILE)}.")
        if declared > store.free():
            raise HTTPException(413, f"Not enough space for {name} ({human(declared)}): {human(store.free())} free. "
                                     "Delete something first.")
    iid = secrets.token_hex(8)
    part = TMP / f"{iid}.part"
    store.reserved[iid] = declared or 0
    written = 0
    try:
        async with await anyio.open_file(part, "wb") as f:
            buf = bytearray()
            async for chunk in request.stream():
                written += len(chunk)
                if written > MAX_FILE:
                    raise HTTPException(413, f"{name} is bigger than the limit per file ({human(MAX_FILE)}).")
                if declared is not None and written > declared:
                    raise HTTPException(400, "The upload was longer than it said it would be.")
                if declared is None:  # no size given up front: hold space as it arrives
                    store.reserved[iid] = written
                    if store.held() > MAX_BYTES or shutil.disk_usage(DATA).free - DISK_MARGIN < len(buf) + len(chunk):
                        raise HTTPException(413, f"Ran out of space while receiving {name}. Delete something first.")
                buf += chunk
                if len(buf) >= CHUNK:
                    await f.write(bytes(buf))
                    buf.clear()
            if buf:
                await f.write(bytes(buf))
        if declared is not None and written != declared:
            raise HTTPException(400, "The upload was cut short. Try again.")
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        item = {"id": iid, "kind": "file", "name": name, "size": written, "type": ctype,
                "created": now(), "pinned": False, "nodownload": False, "parent": None}
        async with store.lock:
            try:   # the folder may have been deleted while this was uploading
                item["parent"] = store.folder(parent)
            except HTTPException:
                pass
            part.replace(FILES / iid)
            store.items.append(item)
            store.save()
        log.info("received %s (%s)", name, human(written))
        return item
    except ClientDisconnect:
        log.info("upload of %s cancelled after %s", name, human(written))
        raise HTTPException(400, "Upload cancelled.")
    finally:
        store.reserved.pop(iid, None)
        part.unlink(missing_ok=True)


class Text(BaseModel):
    text: str
    parent: str | None = None


@app.post("/api/texts")
async def add_text(body: Text, request: Request):
    need_header(request)
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Nothing to share.")
    if len(text.encode("utf-8")) > MAX_TEXT:
        raise HTTPException(413, "That's too long for a text item. Save it as a file and upload it instead.")
    first = text.splitlines()[0]
    item = {"id": secrets.token_hex(8), "kind": "text", "name": first[:80], "size": len(text.encode("utf-8")),
            "type": "text/plain", "text": text, "created": now(), "pinned": False, "nodownload": False}
    async with store.lock:
        item["parent"] = store.folder(body.parent)
        store.items.append(item)
        store.save()
    return item


class NewFolder(BaseModel):
    name: str
    parent: str | None = None


@app.post("/api/folders")
async def add_folder(body: NewFolder, request: Request):
    need_header(request)
    name = clean_name(body.name)[:80]
    async with store.lock:
        parent = store.folder(body.parent)
        if store.depth(parent) >= MAX_DEPTH:
            raise HTTPException(400, "That's too many folders deep.")
        item = {"id": secrets.token_hex(8), "kind": "folder", "name": name, "size": 0, "type": "",
                "created": now(), "pinned": False, "nodownload": False, "parent": parent}
        store.items.append(item)
        store.save()
    return item


class Change(BaseModel):
    name: str | None = None
    parent: str | None = None     # a folder id, or "root" for the top
    nodownload: bool | None = None


@app.patch("/api/items/{iid}")
async def change(iid: str, body: Change, request: Request):
    need_header(request)
    if body.nodownload is not None and not await is_admin(request):
        raise HTTPException(403, "Only the admin can turn downloads on or off. Sign in to the admin panel first.")
    async with store.lock:
        item = store.get(iid)
        if body.name is not None:
            item["name"] = clean_name(body.name)[:80 if item["kind"] == "folder" else 200]
        if body.parent is not None:
            target = store.folder(body.parent)
            if target and item["kind"] == "folder":
                below = [d for d in store.descendants(iid) if d["kind"] == "folder"]
                if target == iid or any(d["id"] == target for d in below):
                    raise HTTPException(400, "A folder can't go inside itself.")
                extra = max([store.depth(d["id"]) - store.depth(iid) for d in below] + [0])
                if store.depth(target) + 1 + extra > MAX_DEPTH:
                    raise HTTPException(400, "That's too many folders deep.")
            item["parent"] = target
        if body.nodownload is not None:
            item["nodownload"] = body.nodownload
        store.save()
    return item


class Pin(BaseModel):
    pinned: bool


@app.post("/api/items/{iid}/pin")
async def pin(iid: str, body: Pin, request: Request):
    need_header(request)
    async with store.lock:
        item = store.get(iid)
        item["pinned"] = body.pinned
        store.save()
    return item


@app.delete("/api/items/{iid}")
async def delete(iid: str, request: Request):
    need_header(request)
    async with store.lock:
        item = store.get(iid)
        gone = [item] + (store.descendants(iid) if item["kind"] == "folder" else [])
        ids = {i["id"] for i in gone}
        store.items = [i for i in store.items if i["id"] not in ids]
        store.save()
        for i in gone:
            if i["kind"] == "file":
                (FILES / i["id"]).unlink(missing_ok=True)
    return {"ok": True, "deleted": len(gone)}


@app.exception_handler(HTTPException)
async def http_error(_req, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


@app.get("/")
async def index():
    return FileResponse(HERE / "static" / "index.html", headers={"Cache-Control": "no-cache"})
