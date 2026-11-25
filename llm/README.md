# Drop: how it's built (reference notes)

This folder is a checked-in reference for LLMs and engineers who start working on this repo
with no prior context. It describes the code as it is, with `file:line` references
(`.dockerignore` lists `llm`, so none of this reaches the image).
If the code and these notes disagree, the code wins. Fix the notes.

## Overview

Drop is the AirplaneMode app for passing files and text snippets between every device on the
travel router's hotspot. It's one small FastAPI process (`app.py`, ~630 lines) plus one
self-contained page (`static/index.html`, plain HTML/CSS/JS, no build step, no external
assets). Files are streamed to disk under `/data/files/<16-hex id>`. All metadata (files, text
snippets and folders) lives in a single `/data/items.json`, rewritten atomically on every
change. There is no Drop login: anyone on the hotspot can look, play and save. The hotspot
password is the access control, and an `X-Drop: 1` header blocks cross-site changes. The one
privileged role is "the admin", recognised by forwarding the platform admin panel's `am_admin`
cookie to the admin service. **On the box Drop is read-only for everyone but the admin**: only
the admin adds, renames, moves, keeps and deletes, until the admin lets everyone do it (a
switch saved in `/data/settings.json`; platform decision 0026). Standalone (no admin service)
everyone can change everything. The admin can also turn downloads off for a file or folder, but
streaming keeps working. Items expire after `DROP_KEEP_DAYS` (default 7) unless someone taps
**keep** on the item or on a folder above it. On the box, the platform's Caddy serves Drop at
`/drop/` with the prefix stripped. The page offers "Play in <app>" for apps that declare
`opens.from == "drop"` in the platform's `apps.json` (today: Cinema, and Doom for `.wad`). Cinema and QuickShot read
`api/items` and stream `api/files/<id>` directly from Drop.

## Files in this folder

| File | What's in it |
|---|---|
| [architecture.md](architecture.md) | Process model, request flow, on-disk layout, config, memory budget, startup/lifespan |
| [data-model.md](data-model.md) | Item kinds and fields, `items.json`, atomic saves, `Store` methods, `load()` migration and cleanup, expiry sweep, space accounting |
| [api.md](api.md) | Every endpoint: inputs, validation, limits, status codes, response shapes; upload streaming and reservation; Range downloads |
| [security.md](security.md) | Threat model, `X-Drop` CSRF defence, inline vs attachment, CSP/nosniff, path safety, admin recognition, who may change Drop (read-only by default), downloads-off semantics (what is and isn't blocked) |
| [page.md](page.md) | `static/index.html`: state, folder navigation (`#f=`), keyed rendering, the move dialog, the uploads queue, admin controls, "Play in <app>" |
| [integration.md](integration.md) | How Drop plugs into the platform (Caddy, compose, apps.json, `/api/apps`, `/api/admin/session`) and how Cinema and QuickShot consume Drop |
| [testing-and-release.md](testing-and-release.md) | The test suite test by test, running locally, Docker image, CI workflow, release process |
| [gotchas-and-extending.md](gotchas-and-extending.md) | Non-obvious behaviour, known rough edges, and checklists for common changes |

## Repo map (outside `llm/`)

```
app.py                 FastAPI app: Store, endpoints, admin check, sweeper
static/index.html      the whole UI (served at GET /)
tests/test_drop.py     API tests, self-running script (no pytest)
requirements.txt       fastapi==0.115.6, uvicorn==0.34.0
Dockerfile             python:3.12-slim, unprivileged uid 10001, VOLUME /data, port 8000
compose.yaml           standalone dev -> http://localhost:8766 (mem_limit 128m)
app.yaml               AirplaneMode app manifest (id, image, port, path, memory, env docs)
.github/workflows/image.yml   tests, then multi-arch image to ghcr.io
.vscode/tasks.json     Standalone up/down, Tests
docs/                  icon.svg, screenshots
CLAUDE.md, README.md   human/Claude notes (rules summary, user-facing description)
```

## Rules you must not break (summary; details in the topic files)

1. Page uses **relative URLs** only (`api/items`, never `/api/items`). A test enforces it.
2. **Never hold a whole file in memory.** The memory limit is 128 MB. Uploads are written in
   1 MB pieces, downloads are read 256 KB at a time.
3. **Never build a filesystem path from client input.** Ids must match `ID_RE`
   (`^[0-9a-f]{16}$`) before touching disk. Names are display-only.
4. **Only `INLINE_TYPES` are served inline.** Everything else is sent as
   `application/octet-stream` + `attachment`, because Drop shares an origin with the admin panel.
5. Every state-changing endpoint calls `await need_editor()` (`X-Drop: 1` + the read-only
   rule) and mutates under `store.lock`, then `store.save()`. The one exception is the admin's
   own switch, `PUT /api/settings` (`need_header` + `is_admin`).
5a. Read-only by default on the box: a missing or unreadable `settings.json` means closed.
   Reading, playing and saving stay open to everyone.
6. Only the admin may change `nodownload`. "Downloads off" must **never** block inline
   streaming or Range requests, or Cinema/QuickShot and the page couldn't play.
7. Keep `api/items` item fields stable (`id`, `kind`, `name`, `parent`, `type`, `size`,
   `created`, `download`). Cinema and QuickShot depend on them.
8. Release tags (`v*`) publish images. Ask Jesse before pushing one.
