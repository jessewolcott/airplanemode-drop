# Gotchas and how to extend

## Gotchas (non-obvious behaviour in the current code)

**Data and expiry**

- **A corrupt or missing `items.json` doesn't delete files, but loses their metadata.**
  `load()` keeps the bad file as `items.json.bad-<time>` and brings every stored file back as
  a pinned `recovered-...` item at the top (`app.py:116-145`). Names, folders and settings are
  gone. Validate the JSON before hand-editing it on a live box.
- **Unpinning, or moving something out of a pinned folder, can delete it within seconds.**
  `created` never changes, so anything older than `KEEP_DAYS` goes on the next
  `GET /api/items` (the page polls every 4 s).
- Folders never expire. Empty folders accumulate until someone deletes them.
- Sweeps run on **every** `GET /api/items`, including Cinema's and QuickShot's backend calls.
  Listing has a side effect.
- `version` restarts at 1 when the process restarts. The page re-renders on any *difference*,
  so a restart where the new version happens to equal the old one would skip one re-render
  (harmless; the next change fixes it).
- `PATCH` always saves, even with an empty body, and bumps `version`.

**Types**

- `type` is guessed from the extension **at upload** with Python's `mimetypes`
  (its built-in table, plus `/etc/mime.types` and similar if the image has them; that wasn't
  checked, so test an extension in the container before relying on it, e.g. `.mkv`).
  `.wad` is registered at startup as `application/x-doom-wad` (`mimetypes.add_type`, next to
  `INLINE_TYPES`); items uploaded before 0.2.1 keep `application/octet-stream`.
  Unknown extensions -> `application/octet-stream`, which is never inline, and never "Play in",
  and blocked entirely by downloads-off. Renaming through the API doesn't recompute `type`.
- The page treats only `png|jpeg|gif|webp|avif|bmp` as images. The server would serve
  `heic/heif` inline too.
- Whether a video plays depends on the viewer's browser (MP4/H.264 is safe, MKV/HEVC often
  isn't). There's no transcoding. "Play in Cinema" is hidden where `canPlayType` says no.

**API edges**

- `parent: null` in `PATCH` means "don't move". Moving to the top needs `"root"` (or `""`).
- A file whose target folder is deleted mid-upload lands at the top silently.
- `PATCH` with `nodownload` from a non-admin returns 403 **before** the 404 check.
- 422 validation errors have a **list** `detail`, so the page's `alert(detail)` would show
  `[object Object]`. The page only sends well-formed bodies, so this rarely shows.
- Multi-range requests get the whole file with 200 (allowed by the RFC). A malformed single
  range gets 416.
- Successful file responses are cacheable for 1 h (`private`). Toggling downloads-off doesn't
  pull back what a browser already cached.
- `download` in `/api/items` is per item, not per viewer. Admins see `false` too.
- Admin answers (including "no") are cached 30 s per token. Sign-out takes up to 30 s to
  take effect in Drop.
- `is_admin` runs on every `GET /api/items`. With the cache that's at most one admin-service
  call per token per 30 s. A down admin service costs up to a 3 s timeout per uncached call
  (in a worker thread, so other requests continue).

**Concurrency and process**

- Single worker only (in-memory `store`, reservations and `version`). Don't add `--workers`.
- Reservations are in memory. A crash drops them, and `load()` deletes the `.part` files.
- The upload size check and the reservation happen without an `await` in between. Keep it that
  way (don't add an `await` between `store.free()` and `store.reserved[iid] = ...`).
- `store.items` is mutated in place by `PATCH`/`pin` while other coroutines may hold references
  from an earlier `by_id()`. Fine in asyncio, as long as nothing awaits in the middle of a
  multi-step change outside the lock.

**Page**

- The page must keep **relative URLs** (`test_page_uses_relative_urls`). Even `href="/..."` fails
  the test.
- Anything that changes an item card's static markup must be part of `sig()`, or the card
  won't rebuild. Anything that changes often (ages, keep state, badges) belongs in `updateNode()`,
  or every poll would rebuild cards and interrupt playing videos.
- The textarea limit counts characters while the server counts UTF-8 bytes.
- The clipboard API is often unavailable on plain-HTTP origins. The page falls back to selecting
  the text.

## How to extend

### Add a stored item field

1. Add it to all three constructors: upload (`app.py:440-441`), text (`433-434`), folder (`455-456`).
2. Migrate old data in `load()` with `i.setdefault("field", default)` (next to `nodownload`,
   `app.py:147-150`).
3. If clients can change it, add it to `Change` (`app.py:503-506`) and handle it in `change()`.
   Decide whether it's admin-only (check `is_admin` before taking the lock, like `nodownload`).
4. If it should inherit down folders, write a helper like `kept()`/`blocked()` over `chain()`,
   and expose a derived field in `items()` (`app.py:320-323`), computing `ids = store.by_id()`
   once.
5. Add a test (and a 0.1-style/old-data case if you migrate). Additions are safe for Cinema and
   QuickShot. Don't rename or retype the fields they use (see [integration.md](integration.md#compatibility-rules-what-not-to-break)).

### Add an endpoint that changes things

- Call `need_header(request)` first.
- Validate ids with `store.get()` and folder ids with `store.folder()`. Never build paths from input.
- Mutate inside `async with store.lock:` and finish with `store.save()`.
- Raise `HTTPException(code, "Human sentence.")`. The page shows `detail` to the user verbatim.
- Add a header-missing case to the tests.

### Serve another type inline

- Add it to `INLINE_TYPES` (`app.py:77`) **only if browsers can't execute it as a document**
  (never HTML, SVG, XML, XHTML or PDF). Keep `nosniff` and the sandbox CSP.
- Update `kindOf()` in the page if it should get a preview, and extend
  `test_dangerous_types_download_as_attachments` or `test_round_trip`.
- Remember that inline types bypass downloads-off. That's the point for media, but it makes
  anything inline effectively "always downloadable" with a little effort.

### Let another app open Drop files

No Drop change is needed: add `opens: {from: "drop", types: [...], url: "/<app>/#...{id}"}` to
that app's entry in `../airplanemode/portal/apps.json` (and its `app.yaml`). The app then
streams from `../drop/api/files/<id>` and should honour `download`.

### Change limits or defaults

- Env defaults live in `app.py:57-60`, and are mirrored in `compose.yaml`, `app.yaml`,
  `README.md`, the platform `compose.yaml` and `hardware/*.env`. Keep them in sync.
- `MAX_DEPTH` is mirrored only in the test that builds a deep chain (`drop.MAX_DEPTH`) and in the
  error text.
- The memory limit (128m) is declared in `app.yaml`, standalone `compose.yaml` and the platform's
  `DROP_MEM`. Streamed I/O is what makes it enough. Never read a whole upload or file into memory.

### UI changes

- Edit `static/index.html` directly (no build). Test in the platform dev stack for admin and
  "Play in" behaviour, and standalone for the no-platform fallback.
- Retake `docs/screenshots/drop.png` and `folder.png` when the UI changes visibly.

### Platform-side changes that affect Drop

- `/api/admin/session` or the `am_admin` cookie format (`TOKEN_RE` assumes urlsafe base64,
  16-128 chars).
- `/api/apps` shape (Drop reads `opens`, `name`).
- Caddy's `handle_path /drop/*` (Drop relies on the prefix being stripped and the page's
  relative URLs).
