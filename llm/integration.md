# Integration: the platform, Cinema and QuickShot

## How Drop plugs into the platform (`../airplanemode`)

Per the app contract (`../airplanemode/docs/app-contract.md`), an app is a pinned image plus
three edits in the platform repo. For Drop:

| Platform file | Drop's entry |
|---|---|
| `compose.yaml` | service `drop`, `image: ghcr.io/jessewolcott/airplanemode-drop:0.3.0` (pinned, never `latest`), env `DROP_MAX_BYTES/DROP_MAX_FILE/DROP_KEEP_DAYS` (defaults 4g/2g/7), `ADMIN_URL: http://admin:8000`, volume `drop-data:/data`, `mem_limit: ${DROP_MEM:-128m}`, `restart: unless-stopped`. No published port. The `proxy` service also mounts `drop-data:/srv/drop:ro` to send file bytes itself (decision 0024). |
| `Caddyfile` | `redir /drop /drop/` and `handle_path /drop/* { reverse_proxy drop:8000 }`. `handle_path` strips `/drop`, so the app sees `/`. |
| `portal/apps.json` | `{"id": "drop", "name": "Drop", "path": "/drop/", "monogram": "Dp", "color": "#5fb8c9", "category": "Tools", "uses_llm": false}` |
| `hardware/*.env` | `nanopi-r6s.env`: `DROP_MEM=128m`, `DROP_MAX_BYTES=4g`, `DROP_MAX_FILE=2g`. `glinet.env`: `DROP_MEM=128m`, `DROP_MAX_BYTES=1g`, `DROP_MAX_FILE=512m`. |

`app.yaml` in this repo is the app's manifest (id, image, port 8000, path `/drop/`, memory
128m, volume `/data`, `needs.llm: none`, env descriptions). CI in the platform repo checks
that the compose image, Caddy route and apps.json entry agree.

The dev stack (`docker compose -f compose.yaml -f compose.dev.yaml up -d --build` in
`../airplanemode`) builds Drop from this sibling folder, at http://localhost:8080/drop/
(admin password `dev`). Standalone dev (`docker compose up -d --build` here) runs Drop alone
on http://localhost:8766 with no admin and no "Play in".

### Platform APIs Drop uses

- **`GET /api/apps`** (browser -> admin service, through Caddy): the installed apps from
  `portal/apps.json` minus the ids the admin hid (`hidden_apps` in the admin service's
  settings). Drop's page calls it as `../api/apps` to find `opens` entries. A hidden app
  disappears from "Play in ..." but keeps running at its address (decision 0013).
- **`GET /api/admin/session`** (Drop backend -> `ADMIN_URL`): 200 if the forwarded `am_admin`
  cookie is a live admin session, 401 otherwise. Details in
  [security.md](security.md#how-the-admin-is-recognised). With `ADMIN_URL` set, Drop is
  read-only for everyone but the admin until the admin opens it up (platform decision 0026,
  [security.md](security.md#who-may-change-drop-read-only-by-default)).

Other apps only **read** from Drop (`GET api/items`, `GET api/files/<id>`), which stays open
to everyone. The one write (QuickShot's setup, below) passes on the person's admin cookie.

### The `opens` contract

An app that can open Drop files declares in `portal/apps.json` (mirrored in its own `app.yaml`):

```json
"opens": {"from": "drop", "types": ["video/*", "audio/*"], "url": "/cinema/#play={id}"}
```

- `from` must be `"drop"`. `types` are MIME patterns (`x/*` or exact). `url` must start with
  `/<lowercase-id>/`, and `{id}` is replaced with the Drop item id.
- For audio/video, Drop shows the button only when this device's browser reports it can play
  the type (`canPlayType`). Other types are offered whenever they match. See
  [page.md](page.md#play-in-app-lines-219-240).
- Today Cinema (`video/*`, `audio/*`) and Doom (`application/x-doom-wad`, url
  `/doom/#drop={id}`) declare `opens`. QuickShot doesn't (it plays a folder, not single files).
- Doom (`../airplanemode-doom`) has no backend: its page fetches `../drop/api/items`, reads
  each `.wad`'s header and directory with two `Range` requests, and downloads the file from
  `../drop/api/files/<id>`. It relies on `kind`, `name`, `id` and Range support.

## Cinema (`../airplanemode-cinema`)

A player app with **no files of its own**: its library is Drop's (decision 0013).

Backend (`airplanemode-cinema/app.py`):

- `DROP_URL` (default `http://drop:8000`) is used server-side: `GET {DROP_URL}/api/items`, no
  cookies, so Drop sees a non-admin, which is irrelevant here. The call triggers Drop's sweep.
- `DROP_PATH` (default `../drop/`) is handed to the browser as `drop_path`.
- `library_from_drop()` reads `items` and keeps:
  - folders: `kind == "folder"` -> `{id, name, parent}`
  - media: `kind == "file"` and `type` matching `^(video|audio)/[A-Za-z0-9.+-]+$` ->
    `{id, name, type, kind: type's major part, size, parent, created, download}`
  - ids must match `^[0-9a-f]{16}$`. Parents that aren't a known folder become top-level.
- Fields Cinema relies on: **`id`, `kind`, `name`, `type`, `parent`, `size`, `created`,
  `download`** (defaults to true if missing). It ignores `kept`, `pinned`, `nodownload`,
  `blocked_by`, `text` and the top-level storage fields.
- Its posters (`PUT api/posters/<id>` on **Cinema**, header `X-Cinema`) are stored in Cinema's
  volume, only for ids Drop currently lists. They're tidied when Drop no longer has the file.

Browser (`airplanemode-cinema/static/index.html`):

- Streams from Drop directly: `src = drop_path + "api/files/" + id` (same origin on the box,
  so the admin cookie and Range work). Cinema never proxies media and runs in 64 MB.
- Deep link from Drop: `#play=<id>` (Drop's opener URL).
- Honours `download: false` with `controlslist="nodownload"` and no context menu, and checks
  `canPlayType` like Drop.
- Its "add in Drop" link goes to `drop_path`.

## QuickShot (`../airplanemode-quickshot`)

A short-video feed over **Drop's top-level folder named "QuickShot"** (env `QUICKSHOT_FOLDER`,
compared case-insensitively after strip). Decision 0014.

Backend (`airplanemode-quickshot/app.py`):

- `_drop(method, path, body=None, cookie=None)` calls `{DROP_URL}{path}` with
  `Content-Type: application/json` and **`X-Drop: 1`** (needed for writes), plus
  `Cookie: am_admin=<cookie>` when given. A 403 from Drop becomes `DropRefused`, which
  QuickShot passes on as 403 with Drop's message (other failures are 503 "Can't reach Drop").
- `GET /api/items`: `find_folder()` = first item with `kind == "folder"`, falsy `parent`
  (top-level), a matching name and a valid id. `clips_in()` = files (`kind == "file"`, `type`
  matching `^video/...`) whose `parent` is that folder or any folder below it (walking
  `parent`). The subfolder right under QuickShot becomes the clip's "creator".
- `POST /api/folders {"name": "QuickShot", "parent": null}` from its `POST api/setup`, when
  the folder is missing, with the caller's `am_admin` cookie forwarded. **This is the only
  write another app makes to Drop.** On a read-only Drop only the admin (or anyone, once the
  admin opened Drop up) can do it; anyone else gets 403 and the page says "Only the admin can
  create it. Sign in to the admin panel first." Putting clips into the folder is a Drop
  upload, so the same rule applies.
- Fields QuickShot relies on: **`id`, `kind`, `name`, `parent`, `type`, `size`, `created`,
  `download`**, and the folder's `name`.
- Likes/views/comments live in QuickShot's own volume. It forgets counts for clip ids no longer
  in the folder (only when the folder was found, so a renamed folder or Drop being down doesn't
  wipe them).

Browser: `v.src = drop_path + "api/files/" + id`, no context menu when `!download`, and an
"Open QuickShot in Drop" link to `drop_path + "#f=" + folder.id` (Drop's hash routing).

## Compatibility rules (what not to break)

- Keep `GET /api/items` -> `{"items": [...]}` with flat items carrying `id`, `kind`
  (`file|text|folder`), `name`, `parent` (folder id or `null`), `type` (MIME), `size`,
  `created` (unix float) and `download` (bool). Adding fields is safe. Renaming or retyping
  these breaks Cinema and QuickShot.
- Keep `GET /api/files/<id>` streaming inline video/audio with Range, **even when
  downloads are off**.
- Keep `POST /api/folders {name, parent}`, the `X-Drop: 1` requirement, and the 403 (with a
  human `detail`) for a non-editor: QuickShot shows Drop's message.
- Keep reading (`api/items`, `api/files`) open to everyone, read-only or not.
- Keep the page's `#f=<id>` hash routing.
- Ids stay 16 lowercase hex chars (both consumers validate with the same regex).
- Renaming the QuickShot folder in Drop "disconnects" QuickShot until it's named QuickShot again
  or QuickShot recreates it.
- Deleting or moving a file in Drop removes it from Cinema/QuickShot on their next poll
  (Cinema reloads its library every 30 s while the player is closed).
