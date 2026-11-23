# Testing, build, CI and release

## Running the tests

```powershell
python tests/test_drop.py        # needs fastapi, uvicorn (requirements.txt) + httpx (for TestClient)
```

`tests/test_drop.py` is a **self-running script, not a pytest suite**. At import it sets
`DATA_DIR` to a fresh temp dir, `DROP_MAX_BYTES=1m`, `DROP_MAX_FILE=600k` and
`DROP_KEEP_DAYS=7` (before importing `app`, because the limits are read at import time). Then
`__main__` opens `with TestClient(drop.app) as c:`, which runs the lifespan (`load`, `sweep`,
sweeper task), and calls every `test_*` function in definition order, printing `ok  <name>`.
The client `c` is a module global defined only under `__main__`, so pytest can't collect these
as written. Most tests start with `reset()`, which deletes every item through the API and
asserts the store is empty. The app needs Python 3.10+ (`str | None` annotations). The
containers use 3.12.

Helpers: `ok(r, code=200)` asserts the status and returns the JSON. `put(name, data, headers=H)`
uploads. `folder(name, parent)` and `patch(iid, **body)`.

## What each test covers

| Test | Covers |
|---|---|
| `test_sizes` | `parse_size`: g/K/decimal m/default/plain bytes |
| `test_page_uses_relative_urls` | The page has `fetch("api/items"`, no `"/api/`, and no `src`/`href` that's external or root-relative |
| `test_page_has_home_button` | Every `a.am-home` (header and move dialog) has `href="../"` and `aria-label="AirplaneMode home"`; the old "AirplaneMode" text link is gone |
| `test_round_trip` | Upload 300 KB jpg -> name/size/type, GET returns the bytes, `accept-ranges`, inline `image/jpeg`; `?dl=1` -> attachment; listing, `used`, `free == cap - size`; `tmp/` is empty |
| `test_dangerous_types_download_as_attachments` | `.html`, `.svg`, `.js` -> `application/octet-stream`, attachment, nosniff, sandbox CSP |
| `test_range` | `bytes=100-199`, `0-1` (iOS), open-ended `102000-`, suffix `-10`, clamped end, unsatisfiable start -> 416 + `bytes */N`, HEAD length and empty body |
| `test_per_file_cap` | 601 KB with Content-Length -> 413 "limit per file", nothing stored |
| `test_total_cap` | 500 K + 400 K stored, 200 K more -> 413 "Not enough space" |
| `test_streaming_cap_without_length` | Chunked (generator) bodies: 700 K -> 413 (per file), nothing left in tmp, `reserved == {}`; with 500 K stored, 576 K chunked -> 413 "space"; 128 K chunked -> OK |
| `test_names_are_never_paths` | `clean_name` cases, stored files named by id only, nothing written outside DATA, bad ids -> 404, RFC 5987 Content-Disposition for `Bordkarte München "1".pdf` |
| `test_text_items` | Text strip/name/no file endpoint (404), blank -> 400, >64 KiB -> 413, texts don't count towards `used` |
| `test_expiry_and_pin` | Backdated 8 days: only the pinned file survives, files unlinked, text expired. Unpinning -> gone on the next listing |
| `test_delete_and_header` | No `X-Drop` -> 403 on PUT/DELETE/POST; delete unlinks the file and bumps `version`; deleting again -> 404 |
| `test_restart_keeps_items_and_cleans_up` | `load()` keeps items, empties tmp, deletes unknown files |
| `test_unreadable_metadata_never_deletes_files` | Corrupt `items.json` -> kept as one `items.json.bad-*`, both files survive as pinned recovered items (mp4 sniffed as `video/mp4` with `.mp4`, the other `application/octet-stream`), a fresh `items.json` is written; a missing `items.json` recovers the same way |
| `test_folders_move_and_delete` | Nested folders, upload/text into folders, parent = file -> 400, unknown -> 404 (before reading the body), move to root and back plus rename, cycle guards, depth limit (8), recursive delete count |
| `test_keep_on_a_folder_keeps_whats_inside` | Pinned folder keeps a child 30 days old (`kept` true, `pinned` false). The folder itself never expires. An outside item expires |
| `test_downloads_off_admin_only_and_inherited` | Fakes `ADMIN_URL` + `check_admin_token`. Non-admin, wrong token and malformed cookie can't set `nodownload`. The admin sets it on a folder -> child `download:false`, `blocked_by`. A non-admin can stream with Range, gets 403 on `?dl=1` and on an attachment-only `.zip`. Moving out unblocks it. The admin can still `?dl=1`. Turning it back on. Admin answers cached (one call for 3 listings) |
| `test_doom_wads_stream_to_the_doom_app` | `.WAD` upload gets `application/x-doom-wad`; Range read returns `PWAD` inline with the sandbox CSP; with downloads off a non-admin can still GET it, `?dl=1` is 403 |
| `test_standalone_has_no_admin` | `ADMIN_URL` empty: `admin_available` false, and setting `nodownload` is 403 even with a cookie |
| `test_old_items_get_folder_fields` | 0.1-style data (no `nodownload`, a dangling `parent`) -> `load()` sets `parent: None`, `nodownload: False` |

Not covered by tests: the page's JS behaviour (keyed rendering, move dialog, uploads queue),
the real `_ask_admin_service` HTTP call, concurrent uploads and the periodic sweeper. Check UI changes by hand in the platform dev stack
(http://localhost:8080/drop/, admin password `dev`) so "Play in Cinema" and the admin show up.
Retake `docs/screenshots/` when the UI changes. Nothing has been tested on real hardware yet
(mark such things *untested*).

## Docker image

- `Dockerfile`: `python:3.12-slim`, deps, app code, user `drop` uid 10001, `/data` owned by it,
  `VOLUME /data`, `EXPOSE 8000`, `CMD uvicorn app:app --host 0.0.0.0 --port 8000`.
- `.dockerignore` keeps out `.git`, `.github`, `.vscode`, `tests`, `__pycache__`, `*.md`,
  `compose.yaml`, `docs/screenshots`, `docs/icon.svg`. The image contains `app.py`,
  `requirements.txt`, `static/`, `app.yaml`, `Dockerfile` and similar.
- `compose.yaml` (standalone): project `drop-dev`, builds `.`, env limits with defaults, volume
  `drop-data`, port `${PORT:-8766}:8000`, `mem_limit: 128m`. **No `ADMIN_URL`**, so no admin.
- `.gitattributes` forces LF for `*.sh`, `*.yaml`, `*.yml` and `Dockerfile` (Windows dev box,
  Linux target).
- VS Code tasks: "Standalone: up", "Standalone: down", "Tests: Drop API" (the default test task).

## CI (`.github/workflows/image.yml`)

Triggers: push to `main`, push of a `v*` tag, and pull requests. Permissions: `contents: read`,
`packages: write`. One job, `build`, on `ubuntu-latest`:

1. `actions/checkout@v7`, `actions/setup-python@v7` (3.12).
2. **API tests**: `pip install -r requirements.txt httpx==0.28.1`, then `python tests/test_drop.py`.
   A failure stops the build.
3. `docker/setup-qemu-action@v4`, `docker/setup-buildx-action@v4`.
4. `docker/login-action@v4` to ghcr.io with `GITHUB_TOKEN` (skipped on PRs).
5. `docker/metadata-action@v6` for `ghcr.io/<owner>/airplanemode-drop` with tags
   `type=ref,event=branch` (-> `:main`) and `type=semver,pattern={{version}}`
   (tag `v1.2.3` -> `:1.2.3`).
6. `docker/build-push-action@v7`: `linux/arm64,linux/amd64`, push except on PRs.

## Release process

1. Make sure the tests pass and the dev stack looks right.
2. Push a `vX.Y.Z` tag. **Ask Jesse first: release tags publish images** (platform and Drop
   CLAUDE.md).
3. CI publishes `ghcr.io/jessewolcott/airplanemode-drop:X.Y.Z` (arm64 + amd64). Make sure the
   package is public.
4. In `../airplanemode`: bump the pinned tag in `compose.yaml` (never `latest`/`main`), and
   update `portal/apps.json` / `Caddyfile` / `hardware/*.env` if anything changed.

Version state (2026-11-22): released 0.1.0, 0.2.0 (folders, downloads off, Play in Cinema),
0.2.1 (Doom WADs, Play in Doom) and 0.2.2 (home button); the platform pins `airplanemode-drop:0.2.2`.
