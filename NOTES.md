# NOTES - design decisions & field findings

## Verified DJI Mimo behavior (2026-10/11, Pocket 4P firmware of that period)

- Auto-upload delivers exactly once per file (camera-side sync record). No repeats observed across 11 days.
- JPG+RAW pair: only the JPG uploads. RAW-only DNG uploads. This is deliberate, not a glitch - re-upload attempts will not change it.
- Upload folders are keyed by **upload date**, not capture date. Late-night shots land in the next day's folder. Capture date must be parsed from the filename (`DJI_YYYYMMDDHHMMSS_NNNN_D.ext` - note date+time are one continuous 14-digit block).
- TIMELAPSE and PANORAMA folders are never uploaded. TIMELAPSE is excluded from backup by design (thousands of source frames).

## Camera file types (complete list handled)

MP4, LRF (proxy - keep with its MP4; camera playback breaks if LRF deleted), JPG, DNG, AAC, WAV (Built-in Mic Audio Backup setting), SRT (only if Video Subtitles enabled), PANORAMA subfolders. Anything unrecognized lands in `OTHER\` and shows in `report` instead of being dropped silently.

## Mic Mini 2S naming and clocks

- Download folders: `Lt_TX_...` = left/blue, `Rt_TX_...` = right/green. Channel must be baked into the filename (`L_`/`R_` prefix) before files leave the download folder, or the pairing info is lost.
- WAV filename: `TX<dev>_MIC<seq>_<YYYYMMDD>_<HHMMSS>_orig.wav` - **recording start time**.
- TX clock syncs only from the linked receiver/camera or the Mimo app. Standalone TXs drift (observed exactly 6 h off on one session, exact-to-the-second on synced sessions).
- `learn_tx_offsets()` votes whole-hour offsets across all video windows per TX; the winning offset is applied before window matching.

## Matching pipeline (why each stage exists)

1. **Min duration 10 s** - TX power-on blips (0.085 s observed) would fake-match.
2. **Window pre-filter** - video `[start, start+dur]` must overlap the (offset-corrected) WAV window.
3. **Cross-correlation gate** - time-window math alone produced false positives inside 30-min WAVs (any 3 s video fits somewhere). Camera scratch audio vs WAV slice, 16 kHz mono, normalized dot product. True pairs scored 0.19-0.20; false 0.01-0.09. Threshold ~0.12-0.15.
4. **Buffered slices** - `video_start-2s` to `video_end+2s`, clamped to WAV bounds; sidecar `.txt` records the exact sync point so DaVinci placement is deterministic.

## Copy safety

- `<dest>.part` + size check + `os.replace` (atomic on same volume).
- `verified` only after BOTH destinations byte-match.
- Single-flight file lock (`msvcrt.locking`) prevents two runs racing the DB.
- Sleep inhibitor (`SetThreadExecutionState`) for long transfers.
- Pre-flight free-space check (need + 10 GB margin) on both D: and G:.
- Card clear: only files whose ledger row says `verified`, only after typed `YES`, only with `--clear`.
- Telegram notify verifies the token's bot username is `red_goon_robot` before sending (token-file routing gotcha from the broader Hermes setup).

## Field incident log

- 2026-11-15 first real usb run: 241 files, 0 failures. Two bugs found and fixed same day:
  - `TYPE_OF.get(ext)` missing the leading dot - every row typed `OTHER`; fixed + DB rows repaired in place.
  - Destination path built without the filename - whole `PHOTO`/`VIDEO` folder paths became single files. Junk files removed, `--force` re-run, 241/241 verified with correct layout.
- The 4 Oct-28 pair DNGs are gone forever (card cleared before any usb sync existed). This loss is the reason the usb+clear workflow exists.
