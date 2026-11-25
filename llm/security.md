# Security

## Threat model in one paragraph

Drop runs on a travel router. Its users are everyone who knows the hotspot password: the owner,
family, travel companions. **There's no Drop login by design**: anyone on the hotspot can list,
play and download everything. **Changing things (add, rename, move, keep, delete) is for the
admin only** on the box, until the admin lets everyone do it (platform decision 0026; see
[below](#who-may-change-drop-read-only-by-default)). What Drop does defend against:

- (a) **Other websites** open in a phone's browser making changes (CSRF).
- (b) **Uploaded content** running as a page on the box's origin. That origin also serves the
  admin panel, which controls the router.
- (c) **Path traversal** through names or ids.
- (d) **Disk/memory exhaustion** through uploads.
- (e) **Casual copying** of big files the admin wants kept on the box ("downloads off").
- (f) **Guests deleting or filling the admin's films** (read-only by default).

It doesn't (and can't) defend against a malicious person on the hotspot deleting things once
the admin has opened Drop up (or in a standalone run), or recording a stream.

## The `X-Drop` header (CSRF defence)

- Every state-changing endpoint (`PUT /api/files`, `POST /api/texts`, `POST /api/folders`,
  `PATCH/DELETE /api/items/{id}`, `POST /api/items/{id}/pin`, `PUT /api/settings`) calls
  `need_header()` (`app.py:286-288`), which requires `X-Drop: 1` exactly, or returns 403. All
  but `PUT /api/settings` do it through `need_editor()`, which then checks who may change Drop.
- Why it works: a cross-site page can only send a custom request header with `fetch`/XHR,
  which triggers a CORS preflight (`OPTIONS`). Drop has no CORS middleware and never answers a
  preflight with `Access-Control-Allow-*`, so the browser never sends the real request. HTML
  forms can't set headers at all. Same-origin pages (Drop's own page) can set the header freely.
- Side effect: any page on the **same origin** (the box's address: other AirplaneMode apps)
  *can* send `X-Drop: 1`. That's accepted: they're all trusted, first-party apps. Backend
  callers (QuickShot's `POST /api/folders`) send the header explicitly, and forward the
  caller's `am_admin` cookie so the editor check sees the real person.
- Reads (`GET /api/items`, `GET /api/files/{id}`) need no header. Cross-site pages can trigger
  GETs but can't read the responses (no CORS).
- The platform's admin service uses the same pattern with `X-AirplaneMode: 1`.

## Inline vs attachment, CSP and nosniff

Drop shares an origin (`http://<box>/`) with the admin panel (`/admin/`, whose `am_admin`
cookie has `path=/`). If an uploaded `evil.html` or `evil.svg` rendered as a page at
`/drop/api/files/<id>`, its script would run with the admin panel's origin: it could call
admin APIs with the admin's cookie (and send `X-AirplaneMode: 1`, since it's same-origin).
Drop therefore layers several defences in `download()` (`app.py:388-436`):

1. **Allow-list of inline types** (`INLINE_TYPES`, `app.py:97-98`):
   `image/(png|jpeg|gif|webp|avif|heic|heif|bmp)`, `audio/*`, `video/*`, `text/plain`, and
   `application/x-doom-wad` (a type no browser renders; on the list so the Doom app can stream
   WADs when downloads are off). Only
   these, and only without `?dl=1`, get `Content-Disposition: inline` with their real type.
   **SVG isn't on the list** (it can carry script). Neither are HTML, XML, JS or PDF.
2. Everything else is served as **`Content-Type: application/octet-stream`** +
   **`Content-Disposition: attachment`**, so browsers save it instead of rendering it.
3. **`X-Content-Type-Options: nosniff`** on every file response, so the browser can't
   "upgrade" octet-stream or text/plain to HTML by sniffing the content.
4. **`Content-Security-Policy: sandbox; default-src 'none'; img-src 'self'; media-src 'self'`**
   on every file response. Even if a response were somehow rendered as a document, `sandbox`
   gives it an opaque origin with scripts disabled, and `default-src 'none'` stops it loading
   anything.
5. The stored `type` comes only from the **file name's extension** (`mimetypes.guess_type`),
   never from content or the client's `Content-Type`. An attacker controls the extension, so
   the allow-list (not the type itself) is the real gate.

Tested by `test_dangerous_types_download_as_attachments` (html, svg, js -> octet-stream,
attachment, nosniff, sandbox CSP).

Text snippets are rendered by the page with `esc()` + `linkify()` (links get
`rel="noopener noreferrer"`, `target=_blank`). They're never served as files.

## Names are never paths

- Files are stored as `/data/files/<id>` where `id = secrets.token_hex(8)`, generated
  server-side. The client's name is metadata only.
- `clean_name()` (`app.py:331-336`): NFC-normalise, keep only the last component after any
  `/` or `\`, remove every Unicode control/format/unassigned character (category `C*`, which
  includes `\0`, `\n`, bidi overrides), strip whitespace and leading/trailing dots, cut to 200
  chars, and fall back to `"file"` if nothing is left. Examples from `test_names_are_never_paths`:
  `../../etc/passwd` -> `passwd`, `..\..\boot.ini` -> `boot.ini`, `..` -> `file`,
  `a\x00b\nc.txt` -> `abc.txt`, `""` and `/` -> `file`.
- Every path parameter goes through `store.get()`, which checks `ID_RE` before anything else.
  `items.json`, `..%2f..`, short or uppercase ids all get 404 and never touch the filesystem.
- `Content-Disposition` carries the name safely: an ASCII fallback (`" \ ;` and non-ASCII ->
  `_`) plus RFC 5987 `filename*=UTF-8''<percent-encoded>`.

## Resource limits

- Per-file cap `MAX_FILE`, total cap `MAX_BYTES`, disk margin 64 MiB, all enforced before
  (Content-Length) **and** during streaming. Space is reserved for in-flight uploads. See
  [api.md](api.md#put-apifiles-upload).
- Text: 64 KiB per snippet. There's **no cap on the number of texts or folders**, and they
  don't count towards `used`. An editor (anyone, once Drop is opened up) could bloat
  `items.json` with many 64 KiB texts. Accepted under the threat model.
- Folder depth: 8.
- Memory: streamed I/O. See [architecture.md](architecture.md#memory-budget-limit-128-mb-appyaml-composeyaml-platform-drop_mem).

## How the admin is recognised

There's no Drop password. "Admin" = whoever is signed in to the AirplaneMode admin panel
(decision 0013 in the platform repo).

- The admin service (`../airplanemode/admin/main.py`) sets cookie `am_admin` =
  `secrets.token_urlsafe(32)` (43 chars of `[A-Za-z0-9_-]`) with `httponly`,
  `samesite=strict`, `path=/`. Drop is on the same host, so the browser sends that cookie to
  `/drop/...` too.
- `is_admin(request)` (`app.py:304-315`):
  1. `token = cookies["am_admin"]`. If `ADMIN_URL` is empty (standalone) or the token doesn't
     match `TOKEN_RE` (`^[A-Za-z0-9_-]{16,128}$`) -> `False` and **no network call**. The
     format check stops header injection or junk being forwarded (`"bad token; x=1"` is tested).
  2. Cache lookup: `_admin_cache[token] = (answer, expires_at)`. A hit within **30 s** returns
     the cached answer. **Negative answers are cached too.**
  3. Otherwise `check_admin_token(token)` in a worker thread. By default that's
     `_ask_admin_service` (`app.py:291-297`): `GET {ADMIN_URL}/api/admin/session` with only
     `Cookie: am_admin=<token>`, 3 s timeout. `True` only on HTTP 200. Any error (401,
     timeout, admin service down) -> `False`.
  4. If the cache has more than 256 entries it's cleared wholesale, then the answer is stored.
- The admin service's `/api/admin/session` is a GET behind `require_admin` (a live, unexpired
  session): 200 `{"ok": true}`, otherwise 401. GETs don't need its `X-AirplaneMode` header.
- `check_admin_token` is a module attribute so tests can swap it (`app.py:300`).
- Consequences: after signing out of the admin panel, Drop may still treat that browser as
  admin for up to 30 s. If the admin service is down, nobody is admin (fail closed): a
  read-only Drop then can't be changed by anyone until it's back. Restarting the admin service
  signs everyone out (its sessions are in memory).

Where admin matters (and only here):

1. Every change (upload, text, folder, rename, move, keep, delete) while Drop isn't opened up:
   `need_editor` -> 403 for non-admins. See the next section.
2. `PUT /api/settings` (opening Drop up) -> 403 for non-admins.
3. `PATCH /api/items/{id}` with `nodownload` -> 403 for non-admins, opened up or not.
4. `GET /api/files/{id}`: the admin bypasses the downloads-off check.
5. `GET /api/items` returns `admin` (the page shows lock buttons, the open-up switch and
   always shows **save**), `can_edit`, `open` and `admin_available` (the page shows the
   "Admin? Sign in" link).

## Who may change Drop (read-only by default)

Platform decision 0026. `can_edit(request)` (`app.py:318-321`) is
`not ADMIN_URL or settings["open"] or await is_admin(request)`:

- **On the box, by default: only the admin.** Everyone else can list, play (Range) and save
  (`?dl=1`, unless downloads are off), but every change gets 403 `"Only the admin can add or
  change things in Drop. ..."` from `need_editor()` (`app.py:324-328`). Upload checks it
  before reading any of the body.
- **The switch:** the admin can let everyone change Drop (`PUT /api/settings {"open": true}`,
  the page's footer checkbox). It's stored in `/data/settings.json` and survives restarts and
  updates. Missing or unreadable file = closed (fail closed).
- **Standalone (no `ADMIN_URL`): open.** Nobody can ever be admin there, so a closed Drop
  would be useless. Same behaviour as before 0026.
- Turning downloads off stays admin-only even when Drop is opened up.
- Other apps that change Drop from their backend (QuickShot's setup) forward the caller's
  `am_admin` cookie, so they get the same answer the person would. Cinema and QuickShot
  otherwise only read.

Tested by `test_read_only_for_everyone_but_the_admin`.

## Downloads off: what is and isn't blocked

`nodownload` (set by the admin on a file or folder; inherited by everything below a folder
through `store.blocked()`) is a **deterrent, not DRM** (decision 0013). The rule in
`download()`: `if not inline and blocked and not admin: 403`.

| Request by a non-admin, for a blocked file | Result |
|---|---|
| `GET /api/files/<id>` for an inline type (video/audio/image/text-plain, Doom WAD) | **Allowed**: 200 or 206, inline. This is how the page's `<video>`, Cinema, QuickShot and Doom play. |
| ... with `Range` (seeking) | **Allowed** (206) |
| `HEAD` of an inline type | Allowed |
| `GET ...?dl=1` (the page's **save** link) | **403** |
| Any request for an attachment-only type (zip, pdf, mkv if not `video/*`...) even without `dl` | **403**: it can only ever be a download |
| `GET /api/items` | Still lists the item, with `download: false` and `blocked_by` |
| Moving the file out of the blocked folder | **Allowed for any editor** (anyone, once Drop is opened up), and it's then downloadable again (tested). Move/rename/delete/pin follow the editor rule, not the admin-only rule. |
| Deleting it | Allowed for any editor |
| The admin, any of the above | Allowed |

What the page adds on top (cosmetic): hides **save** (`it.download || info.admin`), and puts
`controlslist="nodownload"` + `oncontextmenu="return false"` on `<video>`/`<audio>`, and
`oncontextmenu` on image previews. Cinema and QuickShot do the same from `download`.

What it can't stop: anyone can fetch the inline URL with a tool (curl with no `dl`) and save
the stream, or record the screen. The goal is to stop casual "save to phone" copies filling
devices and to keep big files on the box.

Texts: `nodownload` on a text item has no server-side effect (texts have no file endpoint, and
their content is in `/api/items`). The page doesn't offer the lock for texts. A text inside a
blocked folder shows `download: false`, but the page ignores that for texts.

Caching caveat: successful file responses carry `Cache-Control: private, max-age=3600`, so a
browser that fetched an attachment before downloads were turned off may still have a cached
copy for up to an hour.
