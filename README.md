# djisync

Sync manager for the **DJI Osmo Pocket 4P** camera and **DJI Mic Mini 2S** transmitters on Windows.

Three manual commands (no background polling, no watchers) + a SQLite ledger that tracks every file: what it is, where it came from, whether it is backed up, and whether its JPG+RAW pair is complete.

```
D:\pocket4p\scripts\pocket4p_sync.py  usb|refile|mic|report|rescan
D:\pocket4p\pocket4p.db               the ledger
```

## Why

- DJI Mimo auto-upload (camera -> PC over wifi) is convenient but **silently skips the DNG half of JPG+RAW pairs** (verified across multiple shoot days; RAW-only DNGs upload fine). USB copy is the only complete path.
- Mimo sorts uploads into **upload-date** folders, not shoot-date folders. Files must be re-filed by the date inside the filename.
- TX audio backups carry their own clock, which can be hours off if the transmitter was not linked to the receiver. Matching mic WAVs to videos must therefore learn the clock offset, not assume zero.
- Keeping the camera card empty matters: Mimo re-scans everything on the card (battery + heat), and files left on the card can be lost when the card is cleared without a verified backup.

## Layout

```
D:\pocket4p\DCIM\<date>\VIDEO|PHOTO     working copy from USB (fast D:), deletable
G:\pocket4p_back\
    DJI Device\OsmoPocket4P-957E\<date>\   Mimo auto-upload inbox (temporary)
    back_up\<date>\VIDEO|PHOTO\PANORAMA    permanent, re-filed by shoot date
D:\pocket4p\mic\<date>\                   mic WAVs, L_/R_ labeled by channel
G:\pocket4p_back\back_up_mic\<date>\      mic WAV permanent backup
D:\pocket4p\mic\matched\                  per-video L/R audio slices (+2 s buffer)
D:\mic mini 2s\<device folders>\          raw download landing zone from the TXs
```

## Commands

Run on Windows: `py D:\pocket4p\scripts\pocket4p_sync.py <command> [flags]`

### usb - camera -> working + backup
- Detects the camera drive (label `Pocket4P`, or any drive with `DCIM\DJI_001`).
- Copies every new file to **both** `D:\pocket4p\DCIM\<date>\TYPE\` and `G:\pocket4p_back\back_up\<date>\TYPE\`, sorted by the date inside the filename.
- **Pair-DNG backfill**: copies the DNGs that Mimo skipped.
- Skips `TIMELAPSE`, `.partial` files, and anything already verified in the ledger.
- Dedupe = name + size, checked in the DB and on disk before writing.
- Pre-flight disk space check, sleep inhibitor, single-flight lock.
- Completion report to Telegram (redgoon bot).
- `--clear` gates card deletion behind a typed `YES` and refuses unless every camera file is verified in both destinations.

### refile - Mimo inbox -> back_up
- Moves files from the Mimo inbox into `back_up\<date>\TYPE\` (copy, verify size, then delete from inbox). Safe to run while the camera uploads; the inbox stays raw otherwise.

### mic - TX WAVs -> organize + backup + match
- Labels channel into the filename (`L_` = left/blue, `R_` = right/green), organizes by date, backs up to `back_up_mic`.
- **Learns each TX's clock offset** (whole-hour search) from the best video-window fits, then matches videos to WAVs with that offset applied.
- **Cross-correlation gate** (numpy): every candidate is verified by audio fingerprint, not just time math - kills coincidental matches inside long WAVs.
- Cuts per-video L/R slices with **+/-2 s buffer** (clamped to the WAV), named after the video (`DJI_<date><time>__L.wav`), with a `.txt` sidecar recording the exact sync point.
- `--clean-landing` gates deleting the download folders behind typed `YES`.
- WAVs shorter than 10 s are ignored (TX power-on blips).

### report / rescan
- `report`: ledger summary by status, type, date, and incomplete JPG+RAW pairs.
- `rescan`: clears DB destination paths that no longer exist on disk (after manual moves).

## The ledger

SQLite, one row per file (primary key `name + size`):

| column | meaning |
|---|---|
| type | MP4 / LRF / JPG / DNG / AAC / WAV / SRT |
| device | camera / L / R |
| shot_date | from filename, never from mtime |
| src | camera-usb / mimo-inbox / mic-download |
| dest_working / dest_backup | exact paths written |
| mimo_uploaded | yes / no / unknown |
| backup_status | pending / verified (+ verified_at) |

Mic match results live in a second table (`micmatch`: video, wav, offset_s, corr, coverage).

## Mimo auto-upload behavior (verified)

| shot type | uploads? |
|---|---|
| video (MP4+LRF) | yes, both |
| photo JPG-only | yes |
| photo JPG+RAW pair | **JPG only - DNG dropped** |
| photo RAW-only | yes |
| timelapse source frames | never |
| panorama source folder | never |

## Safety rules

- Nothing is ever deleted from camera, working, or backup without passing the size-verified gate and a typed `YES`.
- Copies go to `<dest>.part` first, size-checked, then atomically renamed.
- A file is only marked `verified` after both destinations match byte size.
- Camera deletion additionally requires `--clear` on the usb run.

## Requirements

- Windows, Python 3.10+ (stdlib only; numpy optional for the xcorr gate)
- ffmpeg/ffprobe on disk for mic matching + DNG previews (path constant in the script)
- A Telegram bot token file for notifications (optional)

## Status

- [x] usb sync + pair backfill (241-file real run verified, 0 failures)
- [x] refile inbox (11 date folders re-filed, ~750 files)
- [x] mic organize + clock-offset learning + buffered slices
- [x] SQLite ledger + report/rescan
- [x] Telegram completion notifications via redgoon bot
- [ ] web UI (FastAPI on 127.0.0.1:8775, exposed at `/djisync`) - in progress
