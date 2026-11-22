# Drop (AirplaneMode app): notes for Claude

A shared drop box for everyone on the AirplaneMode hotspot: files (streamed to disk) and text
snippets, with expiry and "keep". FastAPI app in `app.py`, one page in `static/index.html`.

Part of AirplaneMode: the platform repo is `../airplanemode`. Read its `CLAUDE.md` for the
environment notes (H:\Github on exFAT, Docker Desktop, port 8000 reserved, commit attribution)
and `docs/app-contract.md` for the rules this app must follow.

## Commands

```powershell
docker compose up -d --build         # standalone dev -> http://localhost:8766
python tests/test_drop.py            # API tests (CI runs these before building)
```

Inside the platform stack it's served at `/drop/` with the prefix stripped.

## Rules

- The page must use **relative URLs** (`api/items`, not `/api/items`). A test checks it.
- **Never buffer a whole file in memory.** The memory limit is 128m. Uploads stream through
  `request.stream()` in 1 MB writes; downloads stream in 256 KB reads.
- **Never build a path from client input.** Item ids must match `ID_RE` before touching disk.
- **Only types in `INLINE_TYPES` are served inline.** Anything else is an attachment with
  `application/octet-stream`. Drop shares an origin with the platform's admin panel, so an
  uploaded HTML/SVG file must never render as a page.
- Changes need the `X-Drop: 1` header (blocks cross-site requests). There's no login by
  design: the hotspot password is the access control.
- `items.json` is written atomically (temp file + rename) under `store.lock`.

## Releasing

Push a `vX.Y.Z` tag. CI tests, then publishes `ghcr.io/jessewolcott/airplanemode-drop:X.Y.Z`
(arm64 + amd64). **Ask Jesse before pushing a release tag.** Not released yet.
