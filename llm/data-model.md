# Data model

## Items

Everything Drop holds is an **item**: a plain dict in `store.items`, persisted as-is to
`items.json`. Three kinds:

| `kind` | Has a file on disk? | Created by | Extra fields |
|---|---|---|---|
| `"file"` | yes, `/data/files/<id>` | `PUT /api/files` (`app.py:479-480`) | none |
| `"text"` | no (content inline) | `POST /api/texts` (`app.py:513-514`) | `text` |
| `"folder"` | no | `POST /api/folders` (`app.py:535-536`) | none |

### Stored fields (what's in `items.json`)

| Field | Type | file | text | folder | Notes |
|---|---|---|---|---|---|
| `id` | str | x | x | x | `secrets.token_hex(8)`: 16 lowercase hex chars, matches `ID_RE`. Also the file's name on disk. |
| `kind` | str | `"file"` | `"text"` | `"folder"` | |
| `name` | str | x | x | x | Display only. File: `clean_name()`, max 200 chars. Text: first line of the stripped text, max 80 chars. Folder: `clean_name()[:80]`. |
| `size` | int | bytes written | UTF-8 byte length of `text` | `0` | Only `kind=="file"` sizes count towards space. |
| `type` | str | `mimetypes.guess_type(name)` or `application/octet-stream` | `"text/plain"` | `""` | Guessed from the **file name's extension at upload time**. Never sniffed from content, never recomputed on rename. |
| `created` | float | x | x | x | Unix time (`time.time()`). Drives expiry and sorting. Never updated (not on move, rename or pin). |
| `pinned` | bool | x | x | x | "keep". On a folder: keeps everything below it. |
| `nodownload` | bool | x | x | x | Admin-only "downloads off". On a folder: applies to everything below it. Meaningless for texts (see [security.md](security.md)). |
| `parent` | str or null | x | x | x | Id of the containing folder, or `null` for the top ("On the box"). |
| `text` | str | | x | | The snippet, stripped, max 64 KiB UTF-8. |

### Derived fields (only in `GET /api/items` responses, `app.py:353-356`)

| Field | Meaning |
|---|---|
| `kept` | `store.kept(item)`: the item **or any folder above it** is `pinned`. |
| `download` | `store.blocked(item) is None`: neither the item nor any folder above it has `nodownload`. It's per item, **not per viewer**: it stays `false` for the admin too (the page adds `info.admin` separately). |
| `blocked_by` | `null` if downloads are on, or if the item itself has `nodownload`. Otherwise the id of the nearest ancestor folder with `nodownload`. |

These three are computed on every listing and never stored. The upload/text/folder/patch/pin
endpoints return the **stored** dict, without derived fields.

## `items.json` and atomic saves

`Store.save()` (`app.py:178-182`):

```python
tmp = META.with_suffix(".tmp")            # /data/items.tmp
tmp.write_text(json.dumps({"items": self.items}, ensure_ascii=False, indent=1), encoding="utf-8")
tmp.replace(META)                         # atomic rename on POSIX
self.version += 1
```

- Whole-file rewrite on every change. `os.replace` makes the swap atomic, so a crash leaves
  either the old or the new file, never half of one. (It doesn't fsync, so a power cut
  straight after a save could in theory lose the last change, depending on the filesystem.)
- Always called while holding `store.lock`, except inside `sweep()` when that's called from a
  place that already holds the lock, or from `lifespan` before any request is served.
- `version` is in memory only. `load()` sets it to `1`, and each save adds 1. The page
  re-renders when `version` changes (see [page.md](page.md)). It restarts from 1 on every
  process start.

### Order of `store.items`

Items are appended on creation. The list order on disk is creation order (roughly; a slow
upload is appended when it finishes). `GET /api/items` sorts by `-created` (newest first). The
page re-sorts per folder: folders first (by name, natural/numeric), then everything else in
API order (newest first).

## `Store` methods (`app.py:124-258`)

| Method | What it does |
|---|---|
| `load()` | Startup. See below. |
| `save()` | Atomic write + `version += 1`. |
| `used()` | Sum of `size` over `kind=="file"` items. Texts and folders don't count. |
| `held()` | `used()` + all in-flight upload reservations (`sum(reserved.values())`). |
| `free()` | `max(0, min(MAX_BYTES - held(), disk_free - DISK_MARGIN))`. What a new upload may use. |
| `by_id()` | `{id: item}` dict. Built fresh each call; pass it into `chain/kept/blocked` when looping to avoid O(n^2). |
| `chain(item, ids=None)` | Generator: the item, then its parent folder, then that one's parent... up to the top. Has a `seen` set, so a (corrupt) cycle terminates. Stops at a missing parent (`ids.get(None)` is `None`). |
| `kept(item, ids=None)` | `any(i.get("pinned") for i in chain(item))`. Inherited keep. |
| `blocked(item, ids=None)` | First item in `chain(item)` with `nodownload`, or `None`. Returns the item itself if it's directly blocked. Used for `download`/`blocked_by` and the download check. |
| `descendants(folder_id)` | Everything below a folder, at any depth (iterative walk over `parent`). Excludes the folder itself. O(n x folders), fine at this scale. |
| `depth(fid)` | Number of folders in the chain of folder `fid`, i.e. a top-level folder has depth 1, and `None` (the top) has depth 0. Note the docstring says "the top = 0". |
| `folder(fid)` | Validates a folder id **from the client**. `None`, `""` or `"root"` -> `None` (the top). Otherwise `get(fid)` (404 if unknown or malformed) and 400 `"That's not a folder."` if it isn't a folder. Returns the id. |
| `get(iid)` | 404 `"No such item."` unless `iid` matches `ID_RE` **and** exists. The regex check comes first, so junk never gets near a path. |
| `sweep()` | Expiry, see below. Returns the number removed. |

### Depth arithmetic

- Creating a folder: refused if `depth(parent) >= MAX_DEPTH` (`app.py:533`). The top has depth
  0, so the deepest folder has depth 8: `MAX_DEPTH` folders nested. The test creates
  Movies + 7 more (depth 8), and a 9th is refused (`tests/test_drop.py:277-280`).
- Moving folder `F` into `T` (`app.py:557-566`): `extra` = how far F's deepest subfolder is
  below F (`max(depth(d) - depth(F))` over F's descendant folders, or 0). Refused if
  `depth(T) + 1 + extra > MAX_DEPTH`. Moving to the top (`target is None`) skips all checks.
- Cycle guard: refuse if `T == F` or `T` is one of F's descendant folders.
- Files and texts can be moved anywhere. Depth only limits folders.

## `load()`: migration and cleanup (`app.py:131-176`)

In order:

1. `mkdir -p` `files/` and `tmp/`.
2. **Delete every file in `tmp/`.** Partial uploads can't survive a restart.
3. Read `items.json` -> `["items"]`. `FileNotFoundError` -> empty, and **recovering**. Any
   other error (bad JSON, missing `"items"` key...) -> the file is renamed to
   `items.json.bad-<unix time>`, a warning is logged, empty list, and **recovering**.
4. `known` = ids from the JSON, taken **before** the next step.
5. Drop `kind=="file"` entries whose `/data/files/<id>` is missing.
6. Migration from 0.1 (which had no folders): `setdefault("nodownload", False)` on every item.
   Any `parent` that isn't the id of a surviving folder (missing key, `null`, or a folder
   that's gone) becomes `None`. Orphans move to the top; nothing is deleted for having a bad
   parent. `pinned` isn't defaulted: it existed in 0.1.
   When recovering, every file in `files/` with a valid id that isn't in `known` comes back
   as a kept item: `name` `recovered-<first 6 id chars><ext>`, `type` sniffed from its first
   bytes (`sniff`/`MAGIC`, `app.py:107-120`: mp4, webm, mp3, jpeg, png, gif, pdf, ogg; else
   `application/octet-stream` with no extension), size and `created` from the file,
   `pinned: true`, `nodownload: false`, `parent: None`. These ids are added to `known`.
7. Delete files in `files/` whose name isn't in `known` (files with no metadata). After a
   recovery nothing is left to delete, so a bad or missing `items.json` never wipes files.
8. `version = 1`; if anything was recovered, `save()` writes a fresh `items.json`.

The migrated values are only written back to disk on the next `save()`.

> Recovered files lose their names, folders and keep/download settings (they come back
> pinned, at the top). Validate the JSON before hand-editing `items.json` on a live box.

The test `test_restart_keeps_items_and_cleans_up` covers steps 2, 5 and 7,
`test_old_items_get_folder_fields` covers step 6, and
`test_unreadable_metadata_never_deletes_files` covers recovery (corrupt and missing file).

## Expiry: `sweep()` (`app.py:243-258`)

- `cutoff = now() - KEEP_DAYS * 86400`.
- Removes every item where `kind != "folder"`, `created < cutoff` and `not kept(item)`.
  - **Folders never expire**, so empty folders stay until someone deletes them.
  - **Keep is inherited**: anything under a pinned folder (at any depth) is kept. Its own
    `pinned` stays `false` and the API shows `kept: true` (`test_keep_on_a_folder_keeps_whats_inside`).
  - Texts expire like files.
- Unlinks the files of removed `kind=="file"` items, filters the list, `save()`s (only when
  something was removed, so a sweep that finds nothing doesn't bump `version`), and logs.
- Age is measured from `created`, which never changes. **Unpinning** an item (or its folder),
  or **moving** an item out of a pinned folder, makes it eligible straight away if it's older
  than `KEEP_DAYS`. It's removed on the very next listing (`test_expiry_and_pin` relies on
  this). The page asks for no confirmation.

When sweeps run: at startup, every 600 s, and at the start of every `GET /api/items`.

## Space accounting

- `used()` counts only stored files. `held()` adds in-flight reservations. `free()` takes the
  smaller of what's left of `MAX_BYTES` and real disk free space minus 64 MiB.
- Reservations live in `store.reserved` (upload id -> bytes) and are always removed in the
  upload's `finally`. Details in [api.md](api.md#put-apifiles-upload).
- `GET /api/items` returns `used`, `cap` (`MAX_BYTES`), `free`, `max_file`, so the page can
  show the bar and pre-check file sizes.
