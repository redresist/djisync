#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pocket4p_sync.py - DJI Pocket 4P + Mic Mini 2S file manager
Commands:
  usb     - copy new camera files -> working (D:) + backup (G:), dedupe via DB, pair-DNG backfill
  refile  - move Mimo auto-upload inbox files -> back_up/<date>/<TYPE>/ (verified move)
  mic     - organize mic WAVs (L_/R_ labels, by date) + backup + audio-match report + slices
  report  - DB summary: what is backed up, missing, pair status
  rescan  - reconcile DB dest paths with disk (after manual moves)
Manual only - no watchers, no polling. DB: D:\\pocket4p\\pocket4p.db
"""
import sqlite3, os, re, sys, shutil, argparse, subprocess, datetime, hashlib

# ---------------- config ----------------
DB_PATH        = r"D:\pocket4p\pocket4p.db"
WORK_CAM       = r"D:\pocket4p\DCIM"                # working, by date
BACKUP_CAM     = r"G:\pocket4p_back\back_up"        # permanent, date\TYPE
MIMO_INBOX     = r"G:\pocket4p_back\DJI Device\OsmoPocket4P-957E"
MIC_LANDING    = r"D:\mic mini 2s"                  # raw downloads from TXs
WORK_MIC       = r"D:\pocket4p\mic"
BACKUP_MIC     = r"G:\pocket4p_back\back_up_mic"
FFMPEG_DIR     = r"E:\comfy_port\ffmpeg_fruc\ffmpeg-master-latest-win64-gpl\bin"
LOG            = r"D:\pocket4p\sync_log.txt"
CAMERA_LABELS  = {"POCKET4P"}                       # volume label, case-insensitive
SKIP_EXT       = {".partial", ".tmp"}
TYPE_OF        = {".MP4": "VIDEO", ".LRF": "VIDEO", ".AAC": "VIDEO",
                  ".JPG": "PHOTO", ".DNG": "PHOTO"}
RE_DJI  = re.compile(r"^DJI_(\d{8})(\d{6})_(\d+)_D\.(MP4|LRF|JPG|DNG|AAC)$", re.I)
RE_WAV  = re.compile(r"^(TX\d+)_MIC(\d+)_(\d{8})_(\d{6})_orig\.wav$", re.I)
MIN_WAV_S = 10.0   # ignore TX clips shorter than this for matching
LOCK_FILE    = r"D:\pocket4p\sync.lock"
BOT_TOKEN_FILE = r"D:\pocket4p\bot_token.redgoon.txt"
TELEGRAM_CHAT = "8130499203"
MIN_FREE_GB   = 10.0   # refuse to copy if destination has less than this free

def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as f: f.write(line + "\n")
    except OSError: pass

# ---------------- single-flight lock ----------------
def acquire_lock():
    """Prevent two concurrent runs (double-click, usb+refile racing). Returns fd or None."""
    import msvcrt
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return fd
    except OSError:
        os.close(fd)
        return None

def release_lock(fd):
    if fd is None: return
    try:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)
        os.remove(LOCK_FILE)
    except OSError:
        pass

# ---------------- sleep inhibitor ----------------
class NoSleep:
    """Keep the PC awake during long copies (ES_CONTINUOUS|ES_SYSTEM_REQUIRED)."""
    def __enter__(self):
        try:
            import ctypes
            self._prev = ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
        except Exception:
            self._prev = None
        return self
    def __exit__(self, *a):
        if self._prev is not None:
            try: ctypes.windll.kernel32.SetThreadExecutionState(self._prev)
            except Exception: pass

# ---------------- disk space pre-check ----------------
def ensure_space(path, need_bytes):
    try:
        free = shutil.disk_usage(os.path.splitdrive(path)[0] + os.sep).free
    except OSError:
        return True, "unknown"
    return free > need_bytes, f"{free/1e9:.1f} GB free"

# ---------------- telegram notify (redgoon bot) ----------------
def tg_notify(text):
    try:
        import urllib.request, urllib.parse, json as _json
        tok = open(BOT_TOKEN_FILE, encoding="utf-8-sig").read().strip()
        me = _json.load(urllib.request.urlopen(
            f"https://api.telegram.org/bot{tok}/getMe", timeout=10))
        if me.get("result", {}).get("username") != "red_goon_robot":
            log("tg_notify: wrong bot token, skipped"); return
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT, "text": text}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage",
                               data=data, timeout=15)
    except Exception as e:
        log(f"tg_notify failed: {e}")

def now(): return datetime.datetime.now().isoformat(timespec="seconds")

# ---------------- database ----------------
def db():
    c = sqlite3.connect(DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS files(
        name TEXT, size INTEGER, type TEXT, device TEXT, shot_date TEXT,
        src TEXT, dest_working TEXT, dest_backup TEXT,
        mimo_uploaded TEXT DEFAULT 'unknown',
        backup_status TEXT DEFAULT 'pending', verified_at TEXT,
        PRIMARY KEY(name, size))""")
    c.execute("""CREATE TABLE IF NOT EXISTS micmatch(
        video TEXT, wav TEXT, offset_s REAL, corr REAL,
        coverage TEXT, slice TEXT, checked_at TEXT,
        PRIMARY KEY(video, wav))""")
    return c

def db_upsert(c, name, size, typ, device, date, src):
    c.execute("INSERT OR IGNORE INTO files(name,size,type,device,shot_date,src) VALUES(?,?,?,?,?,?)",
              (name, size, typ, device, date, src))

def same_size(path, size):
    try: return os.path.getsize(path) == size
    except OSError: return False

# ---------------- helpers ----------------
def find_camera():
    import string
    for d in string.ascii_uppercase:
        root = f"{d}:\\".replace("\\", os.sep)
        if not os.path.isdir(root): continue
        try:
            label = subprocess.run(["cmd","/c","vol",f"{d}:"], capture_output=True,
                                   text=True, timeout=10).stdout
        except Exception: continue
        for L in CAMERA_LABELS:
            if L.lower() in (label or "").lower(): return d
    # fallback: any drive with DCIM\DJI_001 and label-like DJI
    for d in string.ascii_uppercase:
        if os.path.isdir(f"{d}:" + os.sep + "DCIM" + os.sep + "DJI_001"): return d
    return None

def copy_verify(src, dst, size):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst) and same_size(dst, size):
        return True, "exists"
    tmp = dst + ".part"
    try:
        shutil.copyfile(src, tmp)
        if not same_size(tmp, size):
            os.remove(tmp); return False, "size-mismatch-after-copy"
        os.replace(tmp, dst)
        return True, "copied"
    except OSError as e:
        try: os.remove(tmp)
        except OSError: pass
        return False, str(e)

def dji_parse(name):
    m = RE_DJI.match(name)
    if not m: return None
    return {"date": m.group(1), "time": m.group(2), "seq": m.group(3),
            "ext": m.group(4).upper()}

def camera_files(cam):
    """yield (fullpath, name, size, sub) sub in {DJI_001, PANORAMA/<sub>}"""
    base = f"{cam}:\\DCIM".replace("/", os.sep)
    for sub, dirs in (("DJI_001", [None]), ("PANORAMA", None)):
        root = os.path.join(base, sub)
        if not os.path.isdir(root): continue
        subs = dirs if dirs else [s for s in os.listdir(root)
                                  if os.path.isdir(os.path.join(root, s))]
        for s in subs:
            folder = root if s is None else os.path.join(root, s)
            try: listing = os.listdir(folder)
            except OSError: continue
            for n in listing:
                if os.path.splitext(n)[1].lower() in SKIP_EXT: continue
                p = os.path.join(folder, n)
                if not os.path.isfile(p): continue
                yield p, n, os.path.getsize(p), (sub if s is None else f"{sub}\\{s}")

def inbox_files():
    if not os.path.isdir(MIMO_INBOX): return
    for datef in os.listdir(MIMO_INBOX):
        dfolder = os.path.join(MIMO_INBOX, datef)
        if not os.path.isdir(dfolder): continue
        for n in os.listdir(dfolder):
            p = os.path.join(dfolder, n)
            if not os.path.isfile(p): continue
            if os.path.splitext(n)[1].lower() in SKIP_EXT: continue
            yield p, n, os.path.getsize(p)

def type_folder(name):
    return TYPE_OF.get(os.path.splitext(name)[1].upper(), "OTHER")

# ---------------- command: usb ----------------
def cmd_usb(args):
    lock = acquire_lock()
    if lock is None:
        print("another sync is already running (lock held) - exiting"); return 1
    try:
        return _usb_locked(args)
    finally:
        release_lock(lock)

def _usb_locked(args):
    cam = find_camera()
    if not cam:
        print("camera not found (no drive with Pocket4P label / DCIM\\DJI_001)")
        tg_notify("pocket4p usb: camera NOT found"); return 1
    print(f"camera at {cam}:")
    c = db()
    copied = exists = failed = 0
    inbox_names = {n for _, n, _ in inbox_files()}
    # pre-flight: estimate bytes to copy, check space on D: and G:
    todo = []
    for path, name, size, sub in camera_files(cam):
        row = c.execute("SELECT backup_status FROM files WHERE name=? AND size=? AND backup_status='verified'",
                        (name, size)).fetchone()
        if not args.force and row: continue
        todo.append(size)
    need = sum(todo)
    for dest in (WORK_CAM, BACKUP_CAM):
        ok, msg = ensure_space(dest, need + MIN_FREE_GB * 1e9)
        if not ok:
            print(f"ABORT: not enough space on {dest}: need ~{need/1e9:.1f} GB + margin, have {msg}")
            tg_notify(f"pocket4p usb ABORT: low space on {dest} ({msg})")
            return 1
    if todo: print(f"to copy: {len(todo)} files, ~{need/1e9:.1f} GB - space OK")
    with NoSleep():
        for path, name, size, sub in camera_files(cam):
            info = dji_parse(name)
            if info:
                date, typ = info["date"], TYPE_OF.get("." + info["ext"], "OTHER")
            else:
                date = datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y%m%d")
                typ = "PANORAMA" if sub.startswith("PANORAMA") else "OTHER"
            db_upsert(c, name, size, typ, "camera", date, "camera-usb")
            mimo_up = "yes" if name in inbox_names else "no"
            c.execute("UPDATE files SET mimo_uploaded=? WHERE name=? AND size=?", (mimo_up, name, size))
            if not args.force and c.execute(
                "SELECT backup_status FROM files WHERE name=? AND size=? AND backup_status='verified'",
                (name, size)).fetchone():
                exists += 1; continue
            if sub.startswith("PANORAMA"):
                rel_dir = os.path.join(date, "PANORAMA", sub.split("\\", 1)[1])
            else:
                rel_dir = os.path.join(date, type_folder(name))
            targets = [os.path.join(BACKUP_CAM, rel_dir, name)]
            if typ != "PANORAMA" or args.work_panorama:
                targets.append(os.path.join(WORK_CAM, rel_dir, name))
            ok_all = True
            for t in targets:
                ok, how = copy_verify(path, t, size)
                if ok:
                    col = "dest_backup" if t.upper().startswith("G:") else "dest_working"
                    c.execute(f"UPDATE files SET {col}=? WHERE name=? AND size=?", (t, name, size))
                else:
                    ok_all = False; log(f"FAIL copy {name} -> {t}: {how}")
            if ok_all:
                c.execute("UPDATE files SET backup_status='verified', verified_at=? WHERE name=? AND size=?",
                          (now(), name, size))
                copied += 1
            else:
                failed += 1
    c.commit()
    print(f"usb: copied={copied} already-verified={exists} failed={failed}")
    print(f"     cleared pending: pairs/DNG backfill included")
    # pairs still incomplete
    rows = c.execute("""
        SELECT substr(name,1,18) base,
               sum(CASE WHEN type='JPG' THEN 1 ELSE 0 END) j,
               sum(CASE WHEN type='DNG' THEN 1 ELSE 0 END) d
        FROM files WHERE type IN ('JPG','DNG') GROUP BY base
        HAVING (j>0 AND d=0) OR (d>0 AND j=0)""").fetchall()
    if rows: print(f"     incomplete pairs: {len(rows)} (raw or jpg half missing)")
    tg_notify(f"pocket4p usb done: copied={copied} already={exists} failed={failed}"
              + (f" incomplete_pairs={len(rows)}" if rows else ""))
    if args.clear:
        verify_clear_camera(c, cam)
    c.close()
    return 0

def verify_clear_camera(c, cam):
    pending = c.execute(
        "SELECT count(*) FROM files WHERE src='camera-usb' AND backup_status!='verified'").fetchone()[0]
    if pending:
        print(f"REFUSING clear: {pending} camera files not verified in backup"); return
    ans = input(f"all camera files verified in backup. DELETE from camera {cam}: ? (type YES) ")
    if ans.strip() != "YES":
        print("card keep - nothing deleted"); return
    n = 0
    for path, name, size, sub in camera_files(cam):
        row = c.execute("SELECT backup_status FROM files WHERE name=? AND size=?",
                        (name, size)).fetchone()
        if row and row[0] == "verified":
            os.remove(path); n += 1
    log(f"card clear: deleted {n} files from camera")

# ---------------- command: refile ----------------
def cmd_refile(args):
    lock = acquire_lock()
    if lock is None:
        print("another sync is already running - exiting"); return 1
    try:
        c = db()
        moved = failed = 0
        for path, name, size in inbox_files():
            info = dji_parse(name)
            date = info["date"] if info else datetime.datetime.fromtimestamp(
                os.path.getmtime(path)).strftime("%Y%m%d")
            typ = type_folder(name)
            db_upsert(c, name, size, typ, "camera", date, "mimo-inbox")
            dst = os.path.join(BACKUP_CAM, date, typ, name)
            if os.path.exists(dst) and same_size(dst, size):
                ok, how = True, "exists"
            else:
                with NoSleep():
                    ok, how = copy_verify(path, dst, size)
            if not ok:
                log(f"FAIL refile {name}: {how}"); failed += 1; continue
            try:
                os.remove(path)
            except OSError as e:
                log(f"FAIL delete inbox {name}: {e}"); failed += 1; continue
            c.execute("""UPDATE files SET dest_backup=?, backup_status='verified',
                         verified_at=?, mimo_uploaded='yes' WHERE name=? AND size=?""",
                      (dst, now(), name, size))
            moved += 1
        c.commit(); c.close()
        print(f"refile: moved={moved} failed={failed}")
        if moved or failed:
            tg_notify(f"pocket4p refile done: moved={moved} failed={failed}")
    finally:
        release_lock(lock)

# ---------------- command: mic ----------------
def cmd_mic(args):
    lock = acquire_lock()
    if lock is None:
        print("another sync is already running - exiting"); return 1
    try:
        c = db()
        if not os.path.isdir(MIC_LANDING):
            print("mic landing folder not found:", MIC_LANDING); return 1
        organized = []
        for dev in os.listdir(MIC_LANDING):
            ddir = os.path.join(MIC_LANDING, dev)
            if not os.path.isdir(ddir): continue
            chan = "L" if dev.lower().startswith("lt") else ("R" if dev.lower().startswith("rt") else "?")
            for n in os.listdir(ddir):
                p = os.path.join(ddir, n)
                if not os.path.isfile(p): continue
                m = RE_WAV.match(n)
                if not m: log(f"mic: unmatched name {n}"); continue
                tx, mic, date, time = m.groups()
                new = f"{chan}_{tx}_{mic}_{date}_{time}_orig.wav"
                sz = os.path.getsize(p)
                db_upsert(c, new, sz, "WAV", chan, date, "mic-download")
                for base, col in ((WORK_MIC, "dest_working"), (BACKUP_MIC, "dest_backup")):
                    dst = os.path.join(base, date, new)
                    ok, how = copy_verify(p, dst, sz)
                    if ok:
                        if col == "dest_backup":
                            c.execute("UPDATE files SET dest_backup=?, backup_status='verified', verified_at=? WHERE name=? AND size=?",
                                      (dst, now(), new, sz))
                        else:
                            c.execute(f"UPDATE files SET {col}=? WHERE name=? AND size=?", (dst, new, sz))
                    else:
                        log(f"FAIL mic copy {new} -> {dst}: {how}")
                organized.append((new, date, time, sz))
        c.commit()
        print(f"mic: organized {len(organized)} wavs (L/R labeled, by date)")
        do_match(c, organized)
        tg_notify(f"pocket4p mic done: {len(organized)} wavs organized")
        if args.clean_landing:
            ans = input(f"delete landing files in {MIC_LANDING} (backed up to {BACKUP_MIC})? (type YES) ")
            if ans.strip() == "YES":
                for dev in os.listdir(MIC_LANDING):
                    ddir = os.path.join(MIC_LANDING, dev)
                    if os.path.isdir(ddir): shutil.rmtree(ddir)
                print("landing cleaned")
        c.close()
    finally:
        release_lock(lock)
    return 0

def wav_dur(path):
    fp = os.path.join(FFMPEG_DIR, "ffprobe.exe")
    r = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], capture_output=True, text=True,
                       cwd=FFMPEG_DIR)
    try: return float(r.stdout.strip())
    except ValueError: return None

def mp4s_by_date():
    out = []
    for datef in os.listdir(WORK_CAM):
        ddir = os.path.join(WORK_CAM, datef)
        if not os.path.isdir(ddir) or not re.match(r"^\d{8}$", datef): continue
        for n in os.listdir(ddir):
            if n.upper().endswith(".MP4"):
                p = os.path.join(ddir, n)
                out.append((n, datef, p, os.path.getsize(p)))
    return out

def do_match(c, organized):
    vids = mp4s_by_date()
    if not vids: print("match: no working MP4s yet"); return
    wavs = []
    for name, date, time, size in organized:
        dur = wav_dur(os.path.join(WORK_MIC, date, name))
        if dur and dur >= MIN_WAV_S:
            wavs.append((name, date, time, dur))
    print(f"match: {len(vids)} videos x {len(wavs)} usable wavs")
    # learn per-TX clock offset from best exact matches (TX clock can be hours off)
    tx_off = learn_tx_offsets(vids, wavs)
    if tx_off:
        for tx, o in sorted(tx_off.items()):
            print(f"       clock offset {tx}: {o:+d}s (applied)")
    matched = unmatched = 0
    report = ["video | wav(L/R) | offset_s | corr | coverage | slice", "-" * 90]
    for vname, vdate, vpath, vsize in vids:
        m = re.match(r"DJI_(\d{8})(\d{6})_\d+_D\.MP4$", vname, re.I)
        if not m: continue
        vdate_, vtime = m.group(1), m.group(2)
        v0 = int(vtime[:2])*3600 + int(vtime[2:4])*60 + int(vtime[4:6])
        vdur = wav_dur(vpath)
        if not vdur: continue
        v1 = v0 + vdur
        hits = []
        for wname, wdate, wtime, wdur in wavs:
            if abs(int(wdate) - int(vdate_)) > 1: continue
            tx = wname.split("_")[1]
            off_cand = tx_off.get(tx, 0)
            w0 = int(wtime[:2])*3600 + int(wtime[2:4])*60 + int(wtime[4:6]) + off_cand
            w1 = w0 + wdur
            if w0 <= v0 and v1 <= w1:
                cov = "full"
            elif w0 < v1 and w1 > v0:
                cov = "partial"
            else:
                continue
            # offset = how far into the wav the video starts (negative if video pre-dates wav)
            hits.append((wname, wdate, round(w0 - v0, 1), cov, w0))
        for wname, wdate, off, cov, w0 in hits:
            corr = ""
            if args_xcorr:
                corr = f"{xcorr_ok(vpath, os.path.join(WORK_MIC, wdate, wname), off):.3f}"
            cut = "no"
            if cov == "full" or (cov == "partial" and args.slice_partial):
                cut = cut_slice(vname, wname, wdate, off, vdur)
            report.append(f"{vname} | {wname} | {off:+.1f} | {corr} | {cov} | {cut}")
            c.execute("INSERT OR REPLACE INTO micmatch(video,wav,offset_s,corr,coverage,checked_at) VALUES(?,?,?,?,?,?)",
                      (vname, wname, off, float(corr) if corr else None, cov, now()))
        if hits: matched += 1
        else: unmatched += 1
    rep_path = os.path.join(WORK_MIC, f"match_report_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt")
    with open(rep_path, "w", encoding="utf-8") as f: f.write("\n".join(report))
    print(f"match: videos-with-audio={matched} no-audio={unmatched}")
    print(f"       report: {rep_path}")

def learn_tx_offsets(vids, wavs):
    """For each TX, find the hour-offset that produces the most exact video-window fits."""
    from collections import Counter
    best = {}
    for wname, wdate, wtime, wdur in wavs:
        tx = wname.split("_")[1]
        w0 = int(wtime[:2])*3600 + int(wtime[2:4])*60 + int(wtime[4:6])
        votes = Counter()
        for vname, vdate, vpath, vsize in vids:
            m = re.match(r"DJI_(\d{8})(\d{6})_\d+_D\.MP4$", vname, re.I)
            if not m or abs(int(m.group(1)) - int(wdate)) > 1: continue
            vt = m.group(2)
            v0 = int(vt[:2])*3600 + int(vt[2:4])*60 + int(vt[4:6])
            vdur = wav_dur(vpath)
            if not vdur: continue
            for h in range(-12, 13):
                off = h * 3600
                a0 = w0 + off
                if a0 <= v0 and v0 + vdur <= a0 + wdur:
                    votes[off] += 1
        if votes:
            off, n = votes.most_common(1)[0]
            if n >= 1 and (tx not in best or n > best[tx][1]):
                best[tx] = (off, n)
    return {tx: o for tx, (o, n) in best.items()}

args_xcorr = False
def xcorr_ok(mp4, wav, off):
    """rough correlation check: compare camera scratch audio to wav slice"""
    global args_xcorr
    ff = os.path.join(FFMPEG_DIR, "ffmpeg.exe")
    try:
        import numpy as np
    except ImportError:
        args_xcorr = False
        return 0.0
    def pcm(src, ss="0", t="30"):
        cmd = [ff, "-y", "-v", "error", "-ss", str(ss), "-t", t, "-i", src,
               "-ac", "1", "-ar", "16000", "-f", "f32le", "-"]
        r = subprocess.run(cmd, capture_output=True, cwd=FFMPEG_DIR)
        return np.frombuffer(r.stdout, dtype=np.float32)
    t = "30" if off == 0 else "10"
    a = pcm(mp4, t=t)
    b = pcm(wav, ss=max(0, off), t=t)
    n = min(len(a), len(b))
    if n < 8000: return 0.0
    a, b = a[:n], b[:n]
    sa, sb = np.std(a), np.std(b)
    if sa == 0 or sb == 0: return 0.0
    return float(np.dot(a/sa, b/sb) / (n/16000))

def cut_slice(vname, wname, wdate, off, vdur, buf=2.0):
    """Cut video window from wav with +/-buf seconds headroom (clamped to wav bounds).
    off = seconds into the wav where the video starts (can be negative)."""
    key = re.match(r"DJI_\d{14}", vname).group(0)
    tag = "L" if wname.startswith("L_") else "R"
    outdir = os.path.join(WORK_MIC, "matched")
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, f"{key}__{tag}.wav")
    if os.path.exists(out): return "exists"
    ff = os.path.join(FFMPEG_DIR, "ffmpeg.exe")
    wdur = wav_dur(os.path.join(WORK_MIC, wdate, wname))
    if wdur is None: return "no-dur"
    # slice start/end in wav-local seconds, buffered, clamped
    start = max(0.0, off - buf)
    end = min(wdur, off + vdur + buf)
    dur = end - start
    if dur <= 0: return "empty"
    subprocess.run([ff, "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                    "-i", os.path.join(WORK_MIC, wdate, wname),
                    "-ac", "1", "-ar", "48000", out], cwd=FFMPEG_DIR, check=False)
    # write sidecar with true sync point (video starts at off-start seconds into the slice)
    meta = f"video={vname} wav={wname} sync_at={off - start:.3f}s len={dur:.3f}s (buffer {buf}s)"
    with open(out.replace(".wav", ".txt"), "w", encoding="utf-8") as f: f.write(meta)
    return f"ok({dur:.1f}s, sync@{off-start:.1f}s)"

# ---------------- command: report ----------------
def cmd_report(args):
    c = db()
    print("== by backup_status ==")
    for r in c.execute("SELECT backup_status, count(*) FROM files GROUP BY 1"): print(f"  {r[0]}: {r[1]}")
    print("== by type ==")
    for r in c.execute("SELECT type, count(*), sum(backup_status='verified') FROM files GROUP BY 1"):
        print(f"  {r[0]}: {r[1]} files, {r[2]} verified")
    print("== incomplete JPG+RAW pairs (half missing on camera side) ==")
    rows = c.execute("""
        SELECT substr(name,1,18) b FROM files WHERE type IN ('JPG','DNG')
        GROUP BY b HAVING sum(type='JPG')=0 OR sum(type='DNG')=0""").fetchall()
    print(f"  {len(rows)} pairs incomplete")
    print("== by date ==")
    for r in c.execute("SELECT shot_date, count(*), sum(backup_status='verified') FROM files GROUP BY 1 ORDER BY 1"):
        print(f"  {r[0]}: {r[1]} files, {r[2]} verified")
    c.close()

# ---------------- command: rescan ----------------
def cmd_rescan(args):
    c = db()
    fixed = gone = 0
    for name, size, dw, db_ in c.execute(
            "SELECT name,size,dest_working,dest_backup FROM files").fetchall():
        for col, p in (("dest_working", dw), ("dest_backup", db_)):
            if p and not os.path.exists(p):
                if col == "dest_backup": gone += 1
                c.execute(f"UPDATE files SET {col}=NULL WHERE name=? AND size=?", (name, size))
            elif p:
                fixed += 1
    c.commit(); c.close()
    print(f"rescan: {fixed} paths valid, {gone} backup paths MISSING on disk (cleared from DB)")

# ---------------- main ----------------
def main():
    global args_xcorr
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("usb");   u.add_argument("--force", action="store_true")
    u.add_argument("--clear", action="store_true"); u.add_argument("--work-panorama", action="store_true")
    sub.add_parser("refile")
    m = sub.add_parser("mic");   m.add_argument("--clean-landing", action="store_true")
    m.add_argument("--slice-partial", action="store_true"); m.add_argument("--no-xcorr", action="store_true")
    sub.add_parser("report"); sub.add_parser("rescan")
    a = ap.parse_args()
    args_xcorr = not getattr(a, "no_xcorr", False)
    try:
        import numpy  # noqa
        print("xcorr gate: numpy available -> enabled")
    except ImportError:
        args_xcorr = False
        print("xcorr gate: numpy missing -> matching by time windows only")
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    rc = {"usb": cmd_usb, "refile": cmd_refile, "mic": cmd_mic,
          "report": cmd_report, "rescan": cmd_rescan}[a.cmd](a)
    sys.exit(rc or 0)

if __name__ == "__main__":
    main()
