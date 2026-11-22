# Drop: pass files between every device on the hotspot

An [AirplaneMode](https://github.com/jessewolcott/airplanemode) app. Anyone on your
AirplaneMode hotspot can drop files and notes on the box, and every other phone, tablet and
laptop sees them a few seconds later, whatever the operating system. Nothing leaves the box.

- **Files:** choose or drag several at once, with a progress bar each. Photos show in the
  list, and video and audio play right in the page, with seeking (HTTP Range).
- **Text:** paste a link, an address or a booking code; others tap **copy**.
- **For the flight:** load movies and music before you leave, tap **keep**, and they stay.
  Everything else goes after 7 days.

## Running it

**On the box:** the platform repo runs the released image at `/drop/`.

**On its own, for development:**

```
docker compose up -d --build        # http://localhost:8766
python tests/test_drop.py           # API tests (CI runs these before building)
```

Use another port with `PORT=9000`.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `DROP_MAX_BYTES` | `4g` | total space Drop may use (`k`/`m`/`g` suffixes) |
| `DROP_MAX_FILE` | `2g` | largest single file |
| `DROP_KEEP_DAYS` | `7` | days before an item is deleted, unless someone tapped **keep** |

Files and `items.json` live in one volume at `/data`. Free space shown in the page is the
smaller of what's left of `DROP_MAX_BYTES` and the disk's free space minus 64 MB.

## How it's built

- **Uploads are streamed to disk** (`PUT api/files?name=…`, the raw file as the body), never
  held in memory: a 700 MB upload peaks at about 36 MB of RAM. They're written to
  `/data/tmp` and renamed into place only when complete. Cancelled or cut-off uploads are
  deleted, and leftovers from a restart are cleaned up at startup. Size limits are checked
  from `Content-Length` before anything is written, and again while the data arrives (for
  uploads without a length). Space being uploaded is held, so two uploads at once can't
  overfill the box.
- **Client file names are never used as paths.** Files are stored by a random 16-hex-digit
  id, and the name is only metadata. Downloads send it as RFC 5987 `filename*`.
- **Uploaded files can't run as pages on the box.** Only images (not SVG), audio, video and
  plain text are shown inline. Everything else downloads as `application/octet-stream`, and
  every file response has `X-Content-Type-Options: nosniff` and a `sandbox` CSP. This matters
  because Drop shares an address with the box's admin panel.
- **The page refreshes the list every 4 seconds** but only rebuilds what changed, so a video
  that's playing keeps playing.

## Who can do what

There's no login. **Anyone on the hotspot can see, download, keep and delete everything**,
so the hotspot password is the access control. Changes need an `X-Drop: 1` header, which
other websites can't send without a CORS preflight that Drop never allows, so a web page
open on a phone can't add or delete items.

## Releasing

Push a tag like `v0.1.0`. GitHub Actions runs the tests and publishes
`ghcr.io/jessewolcott/airplanemode-drop:0.1.0` for arm64 and amd64. Make the package public,
then pin the tag in the platform repo's `compose.yaml`.
