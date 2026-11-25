# API

All paths are relative to the app root. On the box the browser uses `/drop/api/...`, which
Caddy strips to `/api/...`. Backends inside the stack use `http://drop:8000/api/...`. The page
uses relative URLs (`api/items`).

## Conventions

- **Errors** are JSON `{"detail": "<human sentence>"}` with the status code. The custom handler
  for `HTTPException` is at `app.py:621-623`. Request-body validation failures (wrong JSON
  types, a missing required field) come from FastAPI's default handler as **422** with
  `{"detail": [ {loc, msg, type, ...}, ... ]}`, a list rather than a string.
- **Changes need `X-Drop: 1`** (`need_header`, `app.py:286-288`). Without it: **403**
  `"Missing request header."`. FastAPI parses and validates the body *before* the handler
  runs, so a request with a bad body and no header gets 422, not 403. See
  [security.md](security.md#the-x-drop-header-csrf-defence).
- **Changes need an editor** (`need_editor`, `app.py:324-328`: `need_header`, then
  `can_edit`, `app.py:318-321`). On the box Drop is read-only for everyone but the admin until
  the admin opens it up (`PUT /api/settings`). Otherwise: **403** `"Only the admin can add or
  change things in Drop. Sign in to the admin panel, or ask the admin to let everyone change
  Drop."`. Standalone (no `ADMIN_URL`) everyone is an editor. See
  [security.md](security.md#who-may-change-drop-read-only-by-default).
- **Ids** in paths go through `store.get()`: anything not matching `^[0-9a-f]{16}$`, or not
  existing -> **404** `"No such item."`.
- **Folder ids** in bodies/queries go through `store.folder()`: `null`/`""`/`"root"` = the
  top. Unknown -> 404. Not a folder -> **400** `"That's not a folder."`.
- No login of its own: the admin is the platform admin panel's session (see
  [security.md](security.md#how-the-admin-is-recognised)). Reading needs nothing.

## Endpoint list

"Editor" = `need_editor`: the admin, or anyone once the admin opened Drop up, or anyone
standalone.

| Method | Path | Needs | Purpose |
|---|---|---|---|
| GET | `/` | | The page (`static/index.html`, `Cache-Control: no-cache`) |
| GET | `/api/items` | | Everything, plus storage figures, the admin flag, `can_edit`, `open` |
| GET, HEAD | `/api/files/{id}` | | Stream or download a file (Range supported) |
| PUT | `/api/files?name=&parent=` | X-Drop, editor | Upload one file (raw body, streamed) |
| POST | `/api/texts` | X-Drop, editor | Share a text snippet |
| POST | `/api/folders` | X-Drop, editor | Make a folder |
| PATCH | `/api/items/{id}` | X-Drop, editor | Rename, move, admin: downloads on/off |
| POST | `/api/items/{id}/pin` | X-Drop, editor | Keep / un-keep |
| DELETE | `/api/items/{id}` | X-Drop, editor | Delete (a folder takes everything under it with it) |
| PUT | `/api/settings` | X-Drop, admin | Let everyone change Drop (`{"open": bool}`) |

---

## `GET /api/items` (`app.py:347-362`)

1. Takes the lock and runs `store.sweep()` (expired items vanish here).
2. Builds the list sorted newest first, each item = stored fields + `kept`, `download`,
   `blocked_by` (see [data-model.md](data-model.md#derived-fields-only-in-get-apiitems-responses-apppy353-356)).
3. Calls `is_admin(request)` (may do a cached HTTP call to the admin service) and works out
   `can_edit` from it (same rule as `can_edit()`).

Response:

```json
{
  "version": 7,
  "items": [ {"id": "...", "kind": "file", "name": "film.mp4", "size": 1000, "type": "video/mp4",
              "created": 1763700000.1, "pinned": false, "nodownload": false, "parent": "<folder id>",
              "kept": false, "download": false, "blocked_by": "<folder id>"}, ... ],
  "admin": false,              // this caller is the platform admin
  "admin_available": true,     // ADMIN_URL is set (i.e. running on the platform)
  "can_edit": false,           // this caller may add, move, rename, keep and delete
  "open": false,               // the admin let everyone change Drop (settings.json)
  "used": 123456,              // bytes of stored files
  "cap": 4294967296,           // DROP_MAX_BYTES
  "free": 4000000000,          // store.free(): what a new upload may use right now
  "max_file": 2147483648,      // DROP_MAX_FILE
  "keep_days": 7.0
}
```

The list is **flat**: the folder tree is rebuilt client-side from `parent`. Text items include
their full `text`, so this response can be large if people paste a lot.

---

## `PUT /api/files?name=<name>&parent=<folder id>` (upload) (`app.py:440-496`)

The request body **is** the file (no multipart). The page sends a `File` object through XHR.
Content-Length is usually set. Chunked bodies without a length are supported too.

Order of operations:

1. `need_editor`. 403 without `X-Drop: 1`, or for a non-editor, **before any of the body is read**.
2. `name = clean_name(name)` (default `"file"`). See [security.md](security.md#names-are-never-paths).
3. `store.folder(parent)`: **fails before reading any of the body** (400/404). Tested at
   `tests/test_drop.py:268-269`.
4. If `Content-Length` is present and all digits (`declared`):
   - `declared > MAX_FILE` -> **413** `"<name> is <size>; the limit per file is <limit>."`
   - `declared > store.free()` -> **413** `"Not enough space for <name> (<size>): <free> free. Delete something first."`
   - Otherwise reserve it: `store.reserved[iid] = declared`. No `await` sits between the
     check and the reservation, so two concurrent uploads can't both pass on the same free space.
5. `iid = secrets.token_hex(8)`. Write to `/data/tmp/<iid>.part` through `anyio.open_file`.
   For each chunk from `request.stream()`:
   - `written > MAX_FILE` -> **413** `"<name> is bigger than the limit per file (...)."`
   - `declared` given and `written > declared` -> **400** `"The upload was longer than it said it would be."`
   - No `declared`: `reserved[iid] = written` (space is held as it arrives). Then
     `held() > MAX_BYTES`, or real disk free minus margin `< len(buf)+len(chunk)`, -> **413**
     `"Ran out of space while receiving <name>. Delete something first."`
   - Append to `buf`. Flush to disk whenever `len(buf) >= CHUNK` (1 MiB).
6. Flush the remainder. If `declared` and `written != declared` -> **400** `"The upload was cut short. Try again."`
7. `type = mimetypes.guess_type(name)[0] or "application/octet-stream"`.
8. Under the lock: re-validate `parent` (**the folder may have been deleted mid-upload**; if so,
   the file silently lands at the top), `part.replace(FILES/iid)`, append, `save()`.
9. Returns the stored item dict (200).

Errors and cleanup:

- `ClientDisconnect` (the browser aborted, e.g. the page's cancel button) -> logged, **400**
  `"Upload cancelled."` (nobody reads it).
- `finally`: **always** drops the reservation and unlinks the `.part`. The tests assert
  `store.reserved == {}` and an empty `tmp/` after failures.
- Leftover `.part` files from a crash are deleted by `load()` at the next start.

### Space accounting during uploads

`free()` = `min(MAX_BYTES - held(), disk_free - 64 MiB)`, and `held()` includes every in-flight
reservation. With a declared length, the whole length is reserved up front. Without one, the
reservation grows chunk by chunk and the limit is enforced as data arrives
(`test_streaming_cap_without_length`). The in-loop check for chunked uploads compares
`held() > MAX_BYTES` (not `free()`), plus a direct disk check.

---

## `GET|HEAD /api/files/{id}` (`app.py:388-436`)

Open to everyone, also when Drop is read-only (only downloads-off limits it).

Query: `dl` (bool, default false). `?dl=1` asks for a download (attachment).

1. `store.get(id)` (404 for malformed/unknown). Not `kind=="file"` -> **404** `"No such file."`
   (texts and folders have no bytes).
2. `size` from `stat()` on disk. `ctype = item["type"]`.
3. `inline = not dl and INLINE_TYPES.match(ctype)`.
4. **Downloads-off check**: if `not inline` and `store.blocked(item)` and the caller isn't the
   admin -> **403** `"Downloads are turned off for this. You can still play it here."`.
   Inline requests (streaming) are never blocked. See [security.md](security.md#downloads-off-what-is-and-isnt-blocked).
5. Headers on every success, and on 416:
   - `Accept-Ranges: bytes`
   - `Content-Disposition: inline|attachment; filename="<ascii fallback>"; filename*=UTF-8''<percent-encoded>` (`content_disposition()`, `app.py:365-367`; non-ASCII and `" \ ;` become `_` in the fallback)
   - `X-Content-Type-Options: nosniff`
   - `Content-Security-Policy: sandbox; default-src 'none'; img-src 'self'; media-src 'self'`
   - `Cache-Control: private, max-age=3600`
   - `Content-Type`: the stored type when inline, otherwise `application/octet-stream`.
5a. **Behind the platform's Caddy** (`app.py:409-412`, platform decision 0024): a `GET` with the
   request header `X-Sendfile-Type: X-Accel-Redirect` (Caddy adds it) gets **200**, no body,
   the headers above plus `X-Accel-Redirect: /files/<id>`. Caddy then serves
   `/srv/drop/files/<id>` itself (the `drop-data` volume, read-only in the proxy) with its own
   Range, ETag and sendfile, copying Content-Type, Content-Disposition, CSP, nosniff and
   Cache-Control. Steps 6-8 then don't run. HEAD, direct calls from other apps (Cinema,
   QuickShot) and Drop on its own (no such header) still use them. The access checks above
   always run first, so **any new access rule must go before this step**. Tested in
   `test_accel_redirect`.
6. Range (`parse_range()`, `app.py:370-385`). Only a single `bytes=` range:
   - No header -> whole file, **200**.
   - `bytes=a-b` -> `[a, min(b, size-1)]` (an end past EOF is clamped).
   - `bytes=a-` -> `[a, size-1]`.
   - `bytes=-n` -> the last `n` bytes. `n == 0` -> bad.
   - `start >= size` or `start > end` -> bad -> **416** with `Content-Range: bytes */<size>`.
   - **Multiple ranges** (`bytes=0-1,5-6`) -> treated as no range, whole file **200**.
   - Any other malformed header (no comma) -> **416**.
   - Whitespace around tokens is tolerated.
   - With a valid range: **206** with `Content-Range: bytes a-b/size`.
   - `Content-Length` is always set. A zero-byte file sends length 0, and any range on it is 416.
7. `HEAD` -> the same status and headers, no body.
8. `GET` -> `StreamingResponse` over an async generator that seeks to `start` and reads
   `min(256 KiB, left)` until `length` bytes are sent.

Range support matters: iOS Safari won't play video without it (it first asks for `bytes=0-1`),
and seeking in `<video>`/Cinema relies on it. Tested in `test_range`.

---

## `POST /api/texts` (`app.py:504-519`)

Body: `{"text": str, "parent": str|null}`.

- `need_editor` (403).
- `text.strip()`. Empty -> **400** `"Nothing to share."`
- Over `MAX_TEXT` (65536) **UTF-8 bytes** -> **413** `"That's too long for a text item. Save it as a file and upload it instead."`
  (The page's textarea `maxlength=65536` counts UTF-16 code units, so multibyte text can pass
  the browser and still hit this.)
- `name` = first line, up to 80 chars. `size` = UTF-8 byte length. `type` = `text/plain`.
- Under the lock: `parent = store.folder(body.parent)`, append, save. Returns the item.
- Text doesn't count against file space (`test_text_items`).

## `POST /api/folders` (`app.py:527-539`)

Body: `{"name": str, "parent": str|null}`.

- `need_editor` (403).
- `name = clean_name(name)[:80]` (so an empty or junk name becomes `"file"`; the page
  refuses blank names first).
- Under the lock: validate `parent`. `depth(parent) >= MAX_DEPTH` -> **400** `"That's too many folders deep."`
- Folder item: `size 0`, `type ""`. Returns it.
- Duplicate names are allowed (nothing enforces uniqueness).

## `PATCH /api/items/{id}` (`app.py:548-570`)

Body (all optional): `{"name": str, "parent": str, "nodownload": bool}`.

1. `need_editor` (403).
2. If `nodownload` is present and the caller isn't the admin -> **403**
   `"Only the admin can turn downloads on or off. Sign in to the admin panel first."`
   (an opened-up Drop doesn't change that). Both checks happen **before** the item lookup,
   so a non-admin gets 403 even for an unknown id.
3. Under the lock, `store.get(id)`, then each present field in turn:
   - `name`: `clean_name(name)`, truncated to 80 (folders) or 200 (others). Any kind can be
     renamed through the API (the page only offers it for folders). **`type` isn't
     recomputed**: renaming `a.mp4` to `a.zip` keeps `video/mp4`.
   - `parent`: `null`/absent means **don't move**. Use `"root"` (or `""`) for the top. Target
     validated with `store.folder()`. For folders: cycle check -> **400** `"A folder can't go inside itself."`,
     depth check -> **400** `"That's too many folders deep."`. See [data-model.md](data-model.md#depth-arithmetic).
   - `nodownload`: set as given.
4. `save()` (even when nothing changed). Returns the stored item.

Partial failure: fields are applied in order on the live dict. If `name` was set and then the
`parent` check raises 400, the in-memory rename has already happened but wasn't saved. It will
be written by the next save. Hard to hit from the page, which sends one field at a time.

## `POST /api/items/{id}/pin` (`app.py:577-584`)

Body: `{"pinned": bool}`. `need_editor` (403). Sets `pinned`, saves, returns the item. On a folder it
keeps everything below it (inherited at sweep time; children's own `pinned` is untouched).

## `DELETE /api/items/{id}` (`app.py:587-599`)

`need_editor` (403). Under the lock: the item plus, for folders, `store.descendants(id)` (everything at any depth)
are removed from the list and saved, **then** their files are unlinked. Returns
`{"ok": true, "deleted": <count>}`. 404 if it's already gone. Any editor may delete. There's no
trash or undo.

## `PUT /api/settings` (`app.py:606-618`)

Body: `{"open": bool}`. The admin's switch "let everyone add, move, keep and delete".

1. `need_header` (403), then `is_admin` -> **403** `"Only the admin can change who may edit
   Drop. Sign in to the admin panel first."`. Standalone nobody is admin, so this is always 403
   there (and not needed: standalone is open anyway).
2. Sets `settings["open"]`, writes `/data/settings.json` atomically (`.tmp` + `replace`).
3. Bumps `store.version`, so polling pages re-render with the new `can_edit`.
4. Returns `{"open": bool}`.

`settings.json` is read once at start (`load_settings()`, `app.py:81-89`). Missing or
unreadable means closed (read-only for non-admins); a bad file is logged, not renamed.

## `GET /` (`app.py:626-628`)

`FileResponse(static/index.html)` with `Cache-Control: no-cache`, so a new image's page
shows up straight away.
