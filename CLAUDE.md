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
  uploaded HTML/SVG file must never render as a page. `.wad` files get
  `application/x-doom-wad` (a type browsers won't render) and count as inline so the Doom app
  (`../airplanemode-doom`) can still stream them when downloads are off.
- Changes need the `X-Drop: 1` header (blocks cross-site requests). There's no login by
  design: the hotspot password is the access control.
- **Admin = the platform admin panel's session.** `is_admin()` forwards the `am_admin` cookie
  (only if it matches `TOKEN_RE`) to `ADMIN_URL/api/admin/session`, cached 30 s. Standalone
  (`ADMIN_URL` empty) nobody is admin. Only the admin may change `nodownload`.
- **Folders** are items with `kind: "folder"`; everything has `parent` (a folder id or null).
  `nodownload` and `pinned` apply to everything below a folder (`store.blocked()`,
  `store.kept()`). Guard moves against cycles and `MAX_DEPTH`. Deleting a folder deletes its
  contents. 0.1 data has no `parent`/`nodownload`; `load()` fills them in.
- **Downloads off never blocks streaming** (inline types, Range): Cinema and the page must
  still play. It refuses `?dl=1` and attachment-only types for non-admins.
- "Play in …" comes from the platform's `GET /api/apps` (apps the admin hasn't hidden) entries
  with `opens.from == "drop"`. Cinema (`../airplanemode-cinema`) reads `api/items`, so keep
  its fields (`kind`, `parent`, `type`, `download`) stable.
- `items.json` is written atomically (temp file + rename) under `store.lock`.

## Releasing

Push a `vX.Y.Z` tag. CI tests, then publishes `ghcr.io/jessewolcott/airplanemode-drop:X.Y.Z`
(arm64 + amd64). **Ask Jesse before pushing a release tag.** Released: 0.1.0, 0.2.0 (folders, downloads off, Play in Cinema).
