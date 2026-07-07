# ice-relay

Fetches DMI Greenland ice charts, shrinks them to sat-phone-sized JPEGs, and
emails them to a boat's Gmail account -- same delivery model as a GRIB
requester, but for ice.

## How it works

Every 3 hours, GitHub Actions runs `ice_relay.py`, which:

1. Checks the Gmail `Ice` label for an unread `ICE RELOAD` email.
2. Lists DMI's live directory index for each source (WA / SE / NCE) and
   compares the latest chart timestamp against `state.json`.
3. For anything new (or if `ICE RELOAD` was seen), downloads the PDF,
   rasterizes it to greyscale, shrinks it under ~50KB, zips it, and emails
   it to the boat address with subject `ICE <SOURCE> <timestamp>`.
4. Commits the updated `state.json` back to the repo so the next run knows
   what's already been sent.

## One-time setup

1. **Gmail app password**: Google Account -> Security -> 2-Step
   Verification -> App passwords. Generate one, don't share it anywhere but
   GitHub secrets.
2. **Gmail filter**: route incoming ICE requests to a label called `Ice`
   (Settings -> Filters and Blocked Addresses -> Create filter -> Subject:
   `"ICE WA" OR "ICE SE" OR "ICE NCE" OR "ICE RELOAD"` -> Apply label `Ice`).
3. **Repo secrets** (Settings -> Secrets and variables -> Actions):
   - `GMAIL_ADDRESS`
   - `GMAIL_APP_PASSWORD`
   - `BOAT_ADDRESS` (optional -- only needed if replies should go somewhere
     other than `GMAIL_ADDRESS` itself)
4. Push this repo to GitHub. The workflow starts running on its own schedule
   once it's on the default branch.

## Testing before you trust it

Run locally first, with real credentials as environment variables:

```bash
pip install -r requirements.txt
export GMAIL_ADDRESS=you@gmail.com
export GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
python ice_relay.py
```

Check the console output for `[WA]`, `[SE]`, `[NCE]` lines showing the
resulting size in KB, and check the boat's inbox for the actual emails.

## Sources

| Code | DMI folder | Covers |
|---|---|---|
| WA | `Greenland_WA` | Whole Greenland overview |
| SE | `SouthEast_RIC` | Cape Farewell -> Skjoldungen / Tasiilaq |
| NCE | `NorthAndCentralEast_RIC` | Skjoldungen -> Scoresbysund |

## Known limitations

- Target size (50KB) is tunable via `TARGET_KB` in `ice_relay.py` -- greyscale
  JPEG compression will blur fine print/labels at this size; the ice
  edge/concentration symbols should still be legible.
- The reload check only runs when the schedule fires, so there's up to a
  3-hour delay between sending `ICE RELOAD` and getting a response.
- MODIS thumbnail sources (Ammassalik, Kangerlussuaq, Umiivik) are not yet
  wired in -- folders are confirmed to exist on DMI's server, to be added
  later.
