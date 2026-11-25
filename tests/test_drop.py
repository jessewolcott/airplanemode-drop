"""Drop API tests.  Run: python tests/test_drop.py"""
import os
import re
import sys
import tempfile
from pathlib import Path

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="drop-test-"), DROP_MAX_BYTES="1m",
                  DROP_MAX_FILE="600k", DROP_KEEP_DAYS="7")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app as drop  # noqa: E402

H = {"X-Drop": "1"}
K = 1024


def ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.text)
    return r.json()


def reset():
    for i in list(drop.store.items):
        c.delete(f"/api/items/{i['id']}", headers=H)
    assert drop.store.items == []


def put(name, data, headers=H):
    return c.put("/api/files", params={"name": name}, content=data, headers=headers)


def test_sizes():
    assert drop.parse_size("4g", "1") == 4 * K ** 3
    assert drop.parse_size("512K", "1") == 512 * K
    assert drop.parse_size("1.5m", "1") == int(1.5 * K * K)
    assert drop.parse_size("", "2g") == 2 * K ** 3
    assert drop.parse_size("100", "1") == 100


def test_page_uses_relative_urls():
    html = c.get("/").text
    assert 'fetch("api/items"' in html and '"/api/' not in html
    # nothing loaded from outside the box: no external or root-relative src/href
    assert not re.search(r'(src|href)="(https?:|//|/[^/])', html), re.findall(r'(?:src|href)="[^"]*"', html)


def test_page_has_home_button():
    """App contract item 10: the AirplaneMode home button, back to the landing page."""
    html = c.get("/").text
    links = re.findall(r'<a class="am-home" href="([^"]*)" aria-label="AirplaneMode home"', html)
    assert links and set(links) == {"../"}, links
    assert 'class="home"' not in html and ">AirplaneMode</a>" not in html   # the old text link is gone


def test_round_trip():
    reset()
    data = os.urandom(300 * K)
    it = ok(put("holiday photo.jpg", data))
    assert it["name"] == "holiday photo.jpg" and it["size"] == len(data) and it["type"] == "image/jpeg"
    r = c.get(f"/api/files/{it['id']}")
    assert r.status_code == 200 and r.content == data
    assert r.headers["accept-ranges"] == "bytes" and r.headers["content-type"] == "image/jpeg"
    assert r.headers["content-disposition"].startswith("inline")
    assert c.get(f"/api/files/{it['id']}", params={"dl": 1}).headers["content-disposition"].startswith("attachment")
    lst = ok(c.get("/api/items"))
    assert [i["id"] for i in lst["items"]] == [it["id"]] and lst["used"] == len(data)
    assert lst["free"] == 1 * K * K - len(data)
    # nothing left behind in tmp
    assert list(drop.TMP.iterdir()) == []


def test_dangerous_types_download_as_attachments():
    reset()
    for name in ["page.html", "logo.svg", "x.js"]:
        it = ok(put(name, b"<script>alert(1)</script>"))
        r = c.get(f"/api/files/{it['id']}")
        assert r.headers["content-type"] == "application/octet-stream", (name, r.headers)
        assert r.headers["content-disposition"].startswith("attachment")
        assert r.headers["x-content-type-options"] == "nosniff" and "sandbox" in r.headers["content-security-policy"]


def test_range():
    reset()
    data = bytes(range(256)) * 400  # 102400 bytes
    iid = ok(put("clip.mp4", data))["id"]
    r = c.get(f"/api/files/{iid}", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206 and r.content == data[100:200], r.status_code
    assert r.headers["content-range"] == f"bytes 100-199/{len(data)}" and r.headers["content-length"] == "100"
    r = c.get(f"/api/files/{iid}", headers={"Range": "bytes=0-1"})        # what iOS Safari asks first
    assert r.status_code == 206 and r.content == data[:2]
    r = c.get(f"/api/files/{iid}", headers={"Range": "bytes=102000-"})
    assert r.status_code == 206 and r.content == data[102000:]
    r = c.get(f"/api/files/{iid}", headers={"Range": "bytes=-10"})
    assert r.status_code == 206 and r.content == data[-10:]
    r = c.get(f"/api/files/{iid}", headers={"Range": "bytes=0-999999999"})  # end clamped
    assert r.status_code == 206 and r.content == data
    r = c.get(f"/api/files/{iid}", headers={"Range": "bytes=999999-"})
    assert r.status_code == 416 and r.headers["content-range"] == f"bytes */{len(data)}"
    r = c.head(f"/api/files/{iid}")
    assert r.status_code == 200 and r.headers["content-length"] == str(len(data)) and r.content == b""


def test_accel_redirect():
    """Behind the platform's Caddy, Drop names the file and Caddy sends the bytes."""
    reset()
    iid = ok(put("clip.mp4", b"v" * 1000))["id"]
    A = {"X-Sendfile-Type": "X-Accel-Redirect"}
    r = c.get(f"/api/files/{iid}", headers={**A, "Range": "bytes=0-1"})
    assert r.status_code == 200 and r.content == b"", r.status_code
    assert r.headers["x-accel-redirect"] == f"/files/{iid}" and r.headers["content-type"] == "video/mp4"
    assert r.headers["content-disposition"].startswith("inline") and "sandbox" in r.headers["content-security-policy"]
    assert c.head(f"/api/files/{iid}", headers=A).headers["content-length"] == "1000"   # HEAD: Drop answers
    assert "x-accel-redirect" not in c.get(f"/api/files/{iid}").headers               # apps calling Drop directly
    assert c.get("/api/files/0123456789abcdef", headers=A).status_code == 404


def test_per_file_cap():
    reset()
    r = put("big.bin", b"x" * (601 * K))
    assert r.status_code == 413 and "limit per file" in r.json()["detail"], r.text
    assert ok(c.get("/api/items"))["items"] == []


def test_total_cap():
    reset()
    ok(put("a.bin", b"a" * (500 * K)))
    ok(put("b.bin", b"b" * (400 * K)))
    r = put("c.bin", b"c" * (200 * K))
    assert r.status_code == 413 and "Not enough space" in r.json()["detail"], r.text
    assert len(ok(c.get("/api/items"))["items"]) == 2


def test_streaming_cap_without_length():
    """No Content-Length (chunked upload): the limits are enforced while the data arrives."""
    reset()

    def chunks(total):
        sent = 0
        while sent < total:
            yield b"z" * (64 * K)
            sent += 64 * K

    r = c.put("/api/files", params={"name": "stream.bin"}, content=chunks(700 * K), headers=H)
    assert r.status_code == 413, r.text
    assert ok(c.get("/api/items"))["items"] == [] and list(drop.TMP.iterdir()) == []
    assert drop.store.reserved == {}
    ok(put("fill.bin", b"f" * (500 * K)))
    r = c.put("/api/files", params={"name": "stream2.bin"}, content=chunks(576 * K), headers=H)
    assert r.status_code == 413 and "space" in r.json()["detail"], r.text
    r = c.put("/api/files", params={"name": "small.bin"}, content=chunks(128 * K), headers=H)
    assert ok(r)["size"] == 128 * K


def test_names_are_never_paths():
    reset()
    for evil, shown in [("../../etc/passwd", "passwd"), ("..\\..\\boot.ini", "boot.ini"), ("..", "file"),
                        ("a\x00b\nc.txt", "abc.txt"), ("", "file"), ("/", "file")]:
        it = ok(put(evil, b"hello"))
        assert it["name"] == shown, (evil, it["name"])
        assert drop.ID_RE.match(it["id"])
    stored = sorted(p.name for p in drop.FILES.iterdir())
    assert all(drop.ID_RE.match(n) for n in stored), stored
    assert not (drop.DATA.parent / "passwd").exists()
    # ids that aren't ours never reach the filesystem
    for bad in ["..%2f..%2fitems.json", "items.json", "0123", "ZZZZZZZZZZZZZZZZ"]:
        assert c.get(f"/api/files/{bad}").status_code == 404, bad
    # unicode names survive in Content-Disposition (RFC 5987)
    it = ok(put("Bordkarte München \"1\".pdf", b"%PDF"))
    cd = c.get(f"/api/files/{it['id']}").headers["content-disposition"]
    assert cd.startswith('attachment; filename="Bordkarte M_nchen _1_.pdf"'), cd
    assert "filename*=UTF-8''Bordkarte%20M%C3%BCnchen%20%221%22.pdf" in cd, cd


def test_text_items():
    reset()
    it = ok(c.post("/api/texts", json={"text": "  Gate B12\nhttps://example.com/boarding  "}, headers=H))
    assert it["kind"] == "text" and it["name"] == "Gate B12" and it["text"].endswith("boarding")
    assert c.get(f"/api/files/{it['id']}").status_code == 404
    assert c.post("/api/texts", json={"text": "   "}, headers=H).status_code == 400
    assert c.post("/api/texts", json={"text": "x" * (64 * K + 1)}, headers=H).status_code == 413
    assert ok(c.get("/api/items"))["used"] == 0  # text doesn't count against file space


def test_expiry_and_pin():
    reset()
    a = ok(put("old.txt", b"old"))
    b = ok(put("kept.txt", b"kept"))
    t = ok(c.post("/api/texts", json={"text": "old note"}, headers=H))
    ok(c.post(f"/api/items/{b['id']}/pin", json={"pinned": True}, headers=H))
    for i in drop.store.items:
        i["created"] -= 8 * 86400
    ids = [i["id"] for i in ok(c.get("/api/items"))["items"]]
    assert ids == [b["id"]], ids
    assert not (drop.FILES / a["id"]).exists() and (drop.FILES / b["id"]).exists()
    assert t["id"] not in ids
    ok(c.post(f"/api/items/{b['id']}/pin", json={"pinned": False}, headers=H))
    assert ok(c.get("/api/items"))["items"] == []


def test_delete_and_header():
    reset()
    it = ok(put("x.txt", b"x"))
    assert put("y.txt", b"y", headers={}).status_code == 403          # no header: refused
    assert c.delete(f"/api/items/{it['id']}").status_code == 403
    assert c.post("/api/texts", json={"text": "hi"}).status_code == 403
    v = ok(c.get("/api/items"))["version"]
    ok(c.delete(f"/api/items/{it['id']}", headers=H))
    assert not (drop.FILES / it["id"]).exists()
    after = ok(c.get("/api/items"))
    assert after["items"] == [] and after["version"] > v
    assert c.delete(f"/api/items/{it['id']}", headers=H).status_code == 404


def test_restart_keeps_items_and_cleans_up():
    reset()
    it = ok(put("stays.txt", b"stays"))
    (drop.TMP / "dead.part").write_bytes(b"half")       # upload cut off by a restart
    (drop.FILES / "0123456789abcdef").write_bytes(b"?")  # file with no metadata
    drop.store.load()
    assert [i["id"] for i in drop.store.items] == [it["id"]]
    assert list(drop.TMP.iterdir()) == [] and not (drop.FILES / "0123456789abcdef").exists()


def test_unreadable_metadata_never_deletes_files():
    """A corrupt or missing items.json must not wipe the box: files are recovered instead."""
    reset()
    film = ok(put("film.mp4", b"\x00\x00\x00\x18ftypmp42" + b"v" * 100))
    note = ok(put("notes.bin", b"plain bytes"))
    drop.META.write_text("{ this is not json", encoding="utf-8")
    drop.store.load()
    bad = [p.name for p in drop.DATA.iterdir() if p.name.startswith("items.json.bad-")]
    assert len(bad) == 1                                              # the damaged file is kept for a look
    got = {i["id"]: i for i in drop.store.items}
    assert set(got) == {film["id"], note["id"]}
    assert (drop.FILES / film["id"]).exists() and (drop.FILES / note["id"]).exists()
    assert got[film["id"]]["type"] == "video/mp4" and got[film["id"]]["name"].endswith(".mp4")
    assert got[note["id"]]["type"] == "application/octet-stream"
    assert all(i["pinned"] for i in got.values())                     # recovered files don't expire on their own
    assert drop.META.exists()                                         # a fresh items.json describes them
    # missing items.json with files on disk: same
    drop.META.unlink()
    drop.store.load()
    assert {i["id"] for i in drop.store.items} == {film["id"], note["id"]}
    for p in drop.DATA.glob("items.json.bad-*"):
        p.unlink()


def folder(name, parent=None):
    return ok(c.post("/api/folders", json={"name": name, "parent": parent}, headers=H))


def patch(iid, **body):
    return c.patch(f"/api/items/{iid}", json=body, headers=H)


def test_folders_move_and_delete():
    reset()
    movies = folder("Movies")
    kids = folder("Kids", movies["id"])
    film = ok(c.put("/api/files", params={"name": "film.mp4", "parent": kids["id"]}, content=b"m" * 100, headers=H))
    assert film["parent"] == kids["id"]
    note = ok(c.post("/api/texts", json={"text": "popcorn", "parent": movies["id"]}, headers=H))
    assert note["parent"] == movies["id"]
    # a file is not a folder; unknown folders are refused before the upload is read
    assert c.put("/api/files", params={"name": "x", "parent": film["id"]}, content=b"x", headers=H).status_code == 400
    assert c.put("/api/files", params={"name": "x", "parent": "0123456789abcdef"}, content=b"x", headers=H).status_code == 404
    # move to the top and back; rename
    assert ok(patch(film["id"], parent="root"))["parent"] is None
    assert ok(patch(film["id"], parent=kids["id"], name="Film night.mp4"))["name"] == "Film night.mp4"
    # a folder can't go inside itself or its own subfolder
    assert patch(movies["id"], parent=movies["id"]).status_code == 400
    assert patch(movies["id"], parent=kids["id"]).status_code == 400
    # depth limit
    f = movies
    for n in range(drop.MAX_DEPTH - 1):
        f = folder(f"d{n}", f["id"])
    assert c.post("/api/folders", json={"name": "too deep", "parent": f["id"]}, headers=H).status_code == 400
    # deleting a folder deletes everything in it, files included
    r = ok(c.delete(f"/api/items/{movies['id']}", headers=H))
    assert r["deleted"] == 4 + drop.MAX_DEPTH - 1, r   # Movies, Kids, the film, the note + the deep chain
    assert drop.store.items == [] and not (drop.FILES / film["id"]).exists()


def test_keep_on_a_folder_keeps_whats_inside():
    reset()
    box = folder("Flight")
    inside = ok(c.put("/api/files", params={"name": "a.mp3", "parent": box["id"]}, content=b"a", headers=H))
    outside = ok(put("b.mp3", b"b"))
    ok(c.post(f"/api/items/{box['id']}/pin", json={"pinned": True}, headers=H))
    for i in drop.store.items:
        i["created"] -= 30 * 86400
    ids = {i["id"]: i for i in ok(c.get("/api/items"))["items"]}
    assert set(ids) == {box["id"], inside["id"]}, ids  # folder never expires; the file is kept by it
    assert ids[inside["id"]]["kept"] and not ids[inside["id"]]["pinned"]
    assert outside["id"] not in ids


def test_downloads_off_admin_only_and_inherited():
    reset()
    admins = {"a" * 32}
    drop.ADMIN_URL, drop.check_admin_token = "http://admin.test", lambda t: t in admins
    drop._admin_cache.clear()
    try:
        movies = folder("Movies")
        film = ok(c.put("/api/files", params={"name": "film.mp4", "parent": movies["id"]}, content=b"v" * 1000, headers=H))
        zipf = ok(c.put("/api/files", params={"name": "extras.zip", "parent": movies["id"]}, content=b"z" * 10, headers=H))
        # not signed in, or a wrong/odd cookie: refused
        assert patch(movies["id"], nodownload=True).status_code == 403
        c.cookies.set("am_admin", "b" * 32)
        assert patch(movies["id"], nodownload=True).status_code == 403
        c.cookies.set("am_admin", "bad token; x=1")
        assert patch(movies["id"], nodownload=True).status_code == 403
        assert not ok(c.get("/api/items"))["admin"]
        # the admin turns downloads off for the folder
        c.cookies.set("am_admin", "a" * 32)
        lst = ok(c.get("/api/items"))
        assert lst["admin"] and lst["admin_available"]
        ok(patch(movies["id"], nodownload=True))
        c.cookies.clear()
        items = {i["id"]: i for i in ok(c.get("/api/items"))["items"]}
        assert not items[film["id"]]["download"] and items[film["id"]]["blocked_by"] == movies["id"]
        assert not items[movies["id"]]["download"] and items[movies["id"]]["blocked_by"] is None
        # everyone else: streaming works (with seeking), saving a copy doesn't
        r = c.get(f"/api/files/{film['id']}", headers={"Range": "bytes=0-99"})
        assert r.status_code == 206 and r.content == b"v" * 100
        assert c.get(f"/api/files/{film['id']}", params={"dl": 1}).status_code == 403
        assert c.get(f"/api/files/{zipf['id']}").status_code == 403      # attachment-only type
        # moving a file out of the folder makes it downloadable again; in again, blocked
        ok(patch(film["id"], parent="root"))
        assert c.get(f"/api/files/{film['id']}", params={"dl": 1}).status_code == 200
        ok(patch(film["id"], parent=movies["id"]))
        # the admin can still save it
        c.cookies.set("am_admin", "a" * 32)
        assert c.get(f"/api/files/{film['id']}", params={"dl": 1}).status_code == 200
        ok(patch(movies["id"], nodownload=False))
        c.cookies.clear()
        assert c.get(f"/api/files/{zipf['id']}").status_code == 200
        # the answer is cached, so the admin service isn't asked on every request
        calls = []
        drop.check_admin_token = lambda t: calls.append(t) or True
        drop._admin_cache.clear()
        c.cookies.set("am_admin", "c" * 32)
        for _ in range(3):
            c.get("/api/items")
        assert len(calls) == 1
    finally:
        c.cookies.clear()
        drop.ADMIN_URL, drop.check_admin_token = "", drop._ask_admin_service
        drop._admin_cache.clear()


def test_doom_wads_stream_to_the_doom_app():
    reset()
    admins = {"a" * 32}
    drop.ADMIN_URL, drop.check_admin_token = "http://admin.test", lambda t: t in admins
    drop._admin_cache.clear()
    try:
        wad = ok(put("MyMod.WAD", b"PWAD" + b"\0" * 60))
        assert wad["type"] == "application/x-doom-wad"
        r = c.get(f"/api/files/{wad['id']}", headers={"Range": "bytes=0-3"})
        assert r.status_code == 206 and r.content == b"PWAD"
        assert r.headers["content-type"].startswith("application/x-doom-wad")
        assert "sandbox" in r.headers["content-security-policy"]
        # downloads off: the Doom app can still read it, saving a copy is refused
        c.cookies.set("am_admin", "a" * 32)
        ok(patch(wad["id"], nodownload=True))
        c.cookies.clear()
        assert c.get(f"/api/files/{wad['id']}").status_code == 200
        assert c.get(f"/api/files/{wad['id']}", params={"dl": 1}).status_code == 403
    finally:
        c.cookies.clear()
        drop.ADMIN_URL = ""


def test_standalone_has_no_admin():
    reset()
    it = ok(put("x.mp4", b"x"))
    c.cookies.set("am_admin", "a" * 32)
    try:
        assert not ok(c.get("/api/items"))["admin_available"]
        assert patch(it["id"], nodownload=True).status_code == 403
    finally:
        c.cookies.clear()


def test_old_items_get_folder_fields():
    reset()
    it = ok(put("old.txt", b"old"))
    raw = drop.META.read_text(encoding="utf-8").replace('"nodownload": false', '"zz": 0').replace('"parent": null', '"parent": "feedfeedfeedfeed"')
    drop.META.write_text(raw, encoding="utf-8")
    drop.store.load()
    got = drop.store.get(it["id"])
    assert got["parent"] is None and got["nodownload"] is False


if __name__ == "__main__":
    with TestClient(drop.app) as c:
        for name, fn in list(globals().items()):
            if name.startswith("test_"):
                fn()
                print("ok  ", name)
