# The page (`static/index.html`)

One file: inline `<style>` and one inline `<script>`. No framework, no build, no external
assets (the favicon is a `data:` SVG). Dark theme only (`color-scheme: dark`). It's served at
`GET /` with `Cache-Control: no-cache`.

**All URLs are relative** (`api/items`, `api/files/<id>`, `../`, `../api/apps`, `../admin/`).
The page lives at `/drop/` on the box and at `/` standalone. `test_page_uses_relative_urls`
fails on any `fetch("/api/`, `src`/`href` starting with `http:`, `https:`, `//` or a single `/`.

## Layout (HTML, lines 118-166)

- Header: title, then the **home button** at its right end: `<a class="am-home" href="../">`
  with the house SVG and `aria-label="AirplaneMode home"` (the platform's app contract item 10;
  it replaced the old "AirplaneMode" text link). 44 x 44 px, panel background, accent focus
  ring, no transition under reduced motion. `main`'s padding uses `env(safe-area-inset-*)`.
- `.send`: a `<label id="drop">` around a hidden `<input type=file multiple id=files>` (click
  or drag-and-drop), and `#textForm` (textarea `maxlength=65536`, "Share text").
- `#uploads`: one progress row per queued file.
- `.listhead`: `#crumbs` breadcrumbs, `#usage` ("X used · Y free"), `#newFolder` ("+ folder").
- `.space` bar (`#spaceBar` width = used/cap).
- `#items`: the list for the current folder.
- Footer: `#foot` (who can see and delete, and the expiry), `#adminNote` (admin text or the
  sign-in link).
- `<dialog id="moveDlg">`: the move picker. Its `.dhead` row holds the title and a second
  `a.am-home`, because a modal dialog makes the header's button unreachable.

## State (line 175)

```js
let info = null,        // last GET api/items response
    version = -1,       // info.version last rendered
    byId = new Map(),   // id -> item, rebuilt in render()
    here = null,        // current folder id (null = top)
    openers = [];       // apps that can play Drop files (from ../api/apps)
const H = { "X-Drop": "1" }, JH = { ...H, "Content-Type": "application/json" };   // lines 171-172
```

## Polling and refresh (lines 327-334, 489-491)

- On load: `go()` (reads the hash), `refresh()`, then `setInterval(refresh, 4000)`.
- `refresh()` fetches `api/items` (`cache: "no-store"`). It does a **full `render()`** only
  when `version` changed, on first load, or when `admin` flipped. Otherwise it just calls
  `updateNode()` on each existing `.item` (ages, "goes in N d" countdowns, keep state).
  Network errors are swallowed ("box restarting").
- After each action (delete, pin, lock, rename, move, new folder, text, finished upload) the
  page calls `refresh()` straight away.

## Folder navigation: `#f=<id>` (lines 207-213, 293-298, 335-340)

- The current folder is in the URL hash: `#f=<16 hex>`. `go()` parses it with
  `/^#f=([0-9a-f]{16})$/`. Anything else means the top. It's bound to `hashchange`, so
  back/forward and bookmarks work.
- Other apps link into Drop folders this way: QuickShot's "Open QuickShot in Drop" uses
  `drop_path + "#f=" + folder.id`. **Keep this hash format stable.**
- Clicking a folder card (anywhere except its buttons/links) sets `location.hash = "f=" + id`.
- `render()` resets `here` to `null` if the folder no longer exists (the hash isn't rewritten).
- `kids(fid)` = items whose `(parent || null) === fid`. `path(fid)` walks `parent` up to 12
  levels for the breadcrumbs. `folderStats(fid)` counts non-folder items and file bytes
  recursively, for a folder's subtitle and the delete confirmation.
- Breadcrumbs: "On the box" (a link to `#` when inside a folder), then each ancestor as
  `#f=<id>`, and the current folder in bold. The drop zone title becomes "Send files to <folder>".

## Keyed rendering (lines 236-326)

The list refreshes every 4 s, but a `<video>` that's playing must not be destroyed. So nodes
are **keyed by item id** and rebuilt only when their markup would change:

- Each `<article class="item">` has `data-id` and `data-sig`.
- `sig(it)` (line 264) = `[name, download, info.admin, openers.length, folder ? hasOtherFolders(it) : 1].join("|")`:
  everything that changes the static markup (title, save button, lock button, play button vs
  inline player, move button).
- `render()`:
  1. Collects existing nodes into `have` (id -> element).
  2. Builds the visible list: folders first, sorted with `localeCompare(..., {numeric: true})`,
     then non-folders in API order (newest first).
  3. For each item: reuse the existing node if `data-sig` matches, otherwise `itemNode(it)`
     and `replaceWith`. Then `updateNode(el, it)` and move it into position only if it isn't
     already there (`insertBefore`, so unchanged nodes aren't detached and a playing video keeps
     playing).
  4. Removes nodes that are no longer in this folder. An empty folder or top shows an `.empty`
     message instead.
  5. Updates the space bar, usage, limits text, footer and admin note.
- `itemNode(it)` (lines 237-262) builds the static markup:
  - Icon: folder SVG, `TXT`, or the file extension (`ext()`, up to 5 chars, uppercased).
  - Actions: **save** (`api/files/<id>?dl=1` with `download`, only for files when
    `it.download || info.admin`), **copy** (texts), **rename** (folders only), **move** (non-folders
    always; folders only if another folder exists or it isn't at the top), **keep**,
    **lock** (admin only, not for texts), **delete**.
  - Body: text -> `<pre>` with `linkify(esc(text))`. Playable media with an opener -> the
    "Play in <app>" banner link. Image -> lazy `<img>` inside a link to the inline URL. Video ->
    `<video controls preload="metadata" playsinline>`. Audio -> `<audio controls preload="none">`.
    Other files: no body. When `download` is false, players get
    `controlslist="nodownload" oncontextmenu="return false"` and images get `oncontextmenu`.
  - The name is set with `textContent` (no HTML injection). Everything interpolated into HTML
    goes through `esc()`.
- `updateNode(el, it)` (lines 266-292) updates the dynamic bits in place: the `kept` border, the
  subtitle (folder: "N items · size · everything kept"; others: "size|text · age · goes in N d
  / kept / kept by its folder"), the amber **no downloads** badge (its tooltip names the
  blocking folder via `blocked_by`), the keep button's text/state/tooltip, and the lock
  button's state.
- `kindOf(it)` (lines 190-197): `image` only for `png|jpeg|gif|webp|avif|bmp`. That's narrower
  than the server's `INLINE_TYPES`, which also has `heic|heif`, so HEIC shows as a plain file
  card. Then `video/*`, `audio/*`, else `file`.

## Actions (one delegated click handler on `#items`, lines 348-374)

`call(url, opts)` shows `alert(detail || "Failed (<status>)")` on a non-2xx response.

| Button | Request |
|---|---|
| delete | `confirm()` (folders: "...and the N items in it"), then `DELETE api/items/<id>` |
| keep | `POST api/items/<id>/pin {pinned: !it.pinned}` |
| lock (admin) | `PATCH api/items/<id> {nodownload: !it.nodownload}` |
| rename (folders) | `prompt()`, then `PATCH {name}` |
| move | opens the move dialog |
| copy (texts) | `navigator.clipboard.writeText`. On failure (plain-HTTP origins often lack the async clipboard) it selects the `<pre>` text instead and shows "selected". |
| + folder | `prompt()`, then `POST api/folders {name, parent: here}` |

Lock button states (`updateNode`): "downloads off" (the item itself), "off (folder)"
(**disabled**, inherited, with a tooltip that names the folder), or "downloads on".

## Move dialog (lines 382-407)

- `openMove(it)` lists "On the box (top)" (`data-to="root"`) and every folder as a tree
  (depth-first, siblings sorted naturally, indented 16 px per level, with a folder emoji).
- Disabled rows: the item's current location (labelled "(here now)"), and for folders the
  folder itself and all its descendant folders (`descendantFolders()`), mirroring the server's
  cycle guard. The depth limit isn't pre-checked: the server's 400 shows as an alert.
- Picking a row closes the dialog and sends `PATCH api/items/<id> {parent: "<id>"|"root"}`.

## Uploads queue (lines 422-478)

- `addFiles(list)` (from the file input or a drop event) creates a `.up` row per file and a job
  `{f, row, xhr, cancelled, parent: here}`. **The target folder is captured when queued**, so
  changing folders afterwards doesn't redirect it.
- Client-side pre-check: `f.size > info.max_file` -> the row turns red ("too big") and is
  removed after 8 s. The server checks again anyway. There's no client-side free-space check:
  the server's 413 message is shown.
- `pump()` sends **one file at a time** (`sending` flag), in order, with `XMLHttpRequest`
  (not fetch) for upload progress: `PUT api/files?name=<encoded>[&parent=<id>]`, header
  `X-Drop: 1`, body = the `File`.
- Progress: bar width and "NN% of <size>". On 200 the row is removed and the list refreshed.
  On error it shows the server's `detail` or "failed (status)", or "connection lost" on a
  network error, and auto-removes after 8 s.
- Cancel: before sending, it marks the job cancelled and removes the row. While sending, it
  calls `xhr.abort()` (the server sees `ClientDisconnect` and cleans up).
- `beforeunload` warns while an upload is in progress.
- **Leaving via the home button** (a document-level click handler on `a.am-home`) asks
  `confirm()` first when uploads are running or queued, or the text box holds unshared text;
  nothing else is lost by leaving (the list lives on the box, the folder is in the hash). On
  "OK" it sets `leaving` so `beforeunload` doesn't ask a second time.

## "Play in <app>" (lines 215-236)

1. At startup: `fetch("../api/apps")` (the platform's list of apps the admin hasn't hidden).
   It keeps entries with `opens.from === "drop"`, an array `opens.types`, and `opens.url`
   matching `/^\/[a-z0-9-]+\//` (a root-relative path into another app on the same box; no
   absolute or `javascript:` URLs). Standalone, `../api/apps` is a 404 on Drop itself, so
   `openers` stays empty and media plays inline.
2. If openers arrive after the first render, `version = -1; refresh()` forces a re-render.
   `openers.length` is part of `sig`, so the nodes rebuild.
3. `openerFor(it)`: files only. Video/audio only when `canPlayHere(it)`, i.e.
   `document.createElement(kind).canPlayType(type) !== ""` on this device; other kinds (Doom
   WADs) skip that check. Then the first opener
   whose `types` match (`"video/*"` is a prefix match, otherwise exact). The link is
   `".." + url.replace("{id}", encodeURIComponent(id))`: from `/drop/`, `../cinema/#play=<id>`
   resolves to `/cinema/#play=<id>`.
4. The banner replaces the inline player: "Play in Cinema", with "Full screen, picks up where
   you left off" (video), "With the rest of this folder" (audio) or "Opens it there"
   (anything else, e.g. "Play in Doom"). Media files this browser can't
   play get no banner and fall back to the inline player.

The page never names Cinema. Any app that declares `opens` in the platform's `apps.json` gets
the button.

## Admin UI

- `info.admin` true: lock buttons on files and folders, **save** always shown, and a footer note
  explaining downloads-off.
- `info.admin_available` and not admin: the footer says "Admin? Sign in" -> `../admin/`.
- Standalone (`admin_available` false): no admin UI at all.
- The admin state is refreshed with every poll (the cookie is re-checked server-side, cached
  30 s), and a change triggers a full render.
