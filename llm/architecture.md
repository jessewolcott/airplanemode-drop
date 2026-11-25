# Architecture

## Shape

```
browser (phone/laptop on the hotspot)
   |  http://<box>/drop/...           (same origin as /admin/, /cinema/, /quickshot/ ...)
   v
Caddy (platform repo)  --  `redir /drop /drop/` + `handle_path /drop/* { reverse_proxy drop:8000 }`
   |  prefix stripped: the app sees /api/items, /api/files/<id>, /
   v
drop container: uvicorn app:app --host 0.0.0.0 --port 8000   (one process, one worker)
   |-- store (in-memory list of item dicts, mirrored to /data/items.json)
   |-- /data/files/<id>        the file bytes, named by id only
   |-- /data/tmp/<id>.part     uploads in progress
   '-- (optional) ADMIN_URL/api/admin/session   is this cookie the admin?
```

Backend-to-backend callers on the internal Docker network: Cinema and QuickShot call
`http://drop:8000/api/items` (QuickShot also `POST /api/folders`). See
[integration.md](integration.md).

## One process, in-memory state

- `store = Store()` (`app.py:241`) is a module-level singleton holding `items` (a list of
  dicts), `version` (an int), `reserved` (upload id -> bytes) and an `asyncio.Lock`.
- **The design assumes exactly one uvicorn worker.** The Dockerfile's `CMD` starts uvicorn
  without `--workers` (`Dockerfile:11`). Several workers would each have their own `store`,
  overwrite each other's `items.json`, and disagree about reservations and `version`.
- All handlers are `async`. Blocking work is pushed off the event loop where it matters:
  file I/O through `anyio.open_file` (uploads/downloads), the admin-service HTTP call through
  `anyio.to_thread.run_sync` (`app.py:291`). `store.save()` (a small JSON write) and
  `shutil.disk_usage` run inline, which is fine at this size.
- Mutations run under `async with store.lock`. Because asyncio is single-threaded, code
  between two `await`s is atomic anyway. That matters for the upload space check, see
  [api.md](api.md#space-accounting-during-uploads).

## Startup and background work

`lifespan()` (`app.py:254-260`):

1. `store.load()`: creates `/data/files` and `/data/tmp`, deletes everything in `tmp`, reads
   `items.json`, migrates and cleans up (see [data-model.md](data-model.md#load-migration-and-cleanup)).
2. `store.sweep()`: deletes expired items right away.
3. Starts `_sweep_forever()` (`app.py:244-251`): every 600 s, takes the lock and sweeps.
   Exceptions are logged and swallowed. Cancelled on shutdown.

A sweep also runs (under the lock) at the start of every `GET /api/items` (`app.py:316-317`).
The page polls that every 4 s, and Cinema/QuickShot call it too, so expiry is effectively
immediate whenever anyone is looking.

`FastAPI(docs_url=None, redoc_url=None, openapi_url=None, ...)` (`app.py:263`): no Swagger,
no OpenAPI JSON.

## Configuration (environment)

| Var | Default | Parsed by | Meaning |
|---|---|---|---|
| `DATA_DIR` | `/data` | `Path` | Root of all state (`app.py:57`). Tests point it at a temp dir. |
| `DROP_MAX_BYTES` | `4g` | `parse_size` | Total bytes of files Drop may hold (`MAX_BYTES`, `app.py:58`). |
| `DROP_MAX_FILE` | `2g` | `parse_size` | Largest single file (`MAX_FILE`, `app.py:59`). |
| `DROP_KEEP_DAYS` | `7` | `float` | Expiry age in days (`KEEP_DAYS`, `app.py:60`). Fractions work. |
| `ADMIN_URL` | empty | `rstrip("/")` | Platform admin service, e.g. `http://admin:8000`. Empty = nobody is admin (`app.py:68`). |

`parse_size(s, default)` (`app.py:48-53`): lower-cases, strips a trailing `b`, falls back to
`default` when empty, accepts `<number>[k|m|g|t]` with binary multiples (1k = 1024). Decimals
are allowed (`1.5m`). Anything else raises `ValueError` at import time, so the container fails
to start on a bad value rather than running with a surprise limit.

Hard-coded constants (`app.py:61-65`, `70`, `76`):

| Name | Value | Use |
|---|---|---|
| `MAX_TEXT` | 64 KiB (UTF-8 bytes) | Largest text snippet |
| `DISK_MARGIN` | 64 MiB | Disk space always left free |
| `CHUNK` | 1 MiB | Upload write size |
| `ID_RE` | `^[0-9a-f]{16}$` | Valid item id (`secrets.token_hex(8)`) |
| `MAX_DEPTH` | 8 | Max folder nesting |
| `ADMIN_COOKIE` | `am_admin` | Cookie name shared with the admin panel |
| `TOKEN_RE` | `^[A-Za-z0-9_-]{16,128}$` | Accepted cookie format before forwarding |
| `INLINE_TYPES` | regex | MIME types served inline (see [security.md](security.md)) |

## On-disk layout (`DATA_DIR`, a named volume on the box: `drop-data`)

```
/data/items.json        {"items": [ ...item dicts... ]}, indent=1, UTF-8, ensure_ascii=False
/data/items.tmp         transient: written then renamed over items.json (Path.with_suffix(".tmp"))
/data/files/<id>        one file per kind=="file" item, exact uploaded bytes, no extension
/data/tmp/<id>.part     upload in progress; renamed into files/ on success, deleted otherwise
```

Text snippets and folders have no file on disk. A text's content is stored inline in
`items.json` (field `text`).

## Memory budget (limit 128 MB: `app.yaml`, `compose.yaml`, platform `DROP_MEM`)

- Uploads: `request.stream()` chunks are appended to a `bytearray` and flushed to disk each
  time it reaches `CHUNK` (1 MiB) (`app.py:420-436`). Peak extra memory is about 1 MiB plus
  one incoming chunk. The README says a 700 MB upload peaks around 36 MB RSS for the whole
  process.
- Downloads: an async generator reads `min(256 KiB, remaining)` at a time (`app.py:390-399`). Behind the platform's Caddy, Caddy sends the bytes instead (`X-Accel-Redirect`, see [api.md](api.md); platform decision 0024).
- Metadata: the whole item list is in memory and re-serialised on every save. That's fine for
  hundreds or thousands of items. Text items can each carry up to 64 KiB inline, so many large
  texts make `items.json` and every save bigger.

## Container

`Dockerfile`: `python:3.12-slim`, `pip install` of the two pinned deps, `COPY . .` (filtered by
`.dockerignore`: no tests, `.github`, `.vscode`, `*.md`, `compose.yaml`, screenshots, icon), user
`drop` (uid 10001), `/data` created and chowned **before** `VOLUME /data`. A fresh named volume
therefore inherits that owner and the unprivileged process can write to it. Port 8000 inside.
That's not the Windows-reserved host port 8000: the standalone compose maps host 8766, and on
the box the port isn't published at all.
