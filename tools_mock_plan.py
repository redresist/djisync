# Mockup dry-run: compute copy plan from camera without copying. English output only.
import os, re, datetime
from collections import defaultdict
RE_DJI = re.compile(r"^DJI_(\d{8})(\d{6})_(\d+)_D\.(MP4|LRF|JPG|DNG|AAC|WAV|SRT)$", re.I)
TYPE_OF = {".MP4":"VIDEO",".LRF":"VIDEO",".AAC":"VIDEO",".WAV":"VIDEO",".SRT":"VIDEO",
           ".JPG":"PHOTO",".DNG":"PHOTO"}
BASE = r"I:\DCIM\DJI_001"
plan = defaultdict(lambda: [0,0])
pairs = defaultdict(lambda: defaultdict(int))
skipped = []
for n in os.listdir(BASE):
    p = os.path.join(BASE, n)
    if not os.path.isfile(p): continue
    m = RE_DJI.match(n)
    if m:
        date = m.group(1); typ = TYPE_OF.get("." + m.group(4).upper(), "OTHER")
        pairs[(date, n[:18])][m.group(4).upper()] += 1
    else:
        date = datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y%m%d")
        typ = "OTHER"; skipped.append(n)
    rel = os.path.join(date, typ)
    plan[rel][0] += 1; plan[rel][1] += os.path.getsize(p)
print("=== G: backup plan (from DJI_001) ===")
for k in sorted(plan):
    print(f"  G:\\pocket4p_back\\back_up\\{k}  <- {plan[k][0]} files  {plan[k][1]/1e9:.2f} GB")
print("=== D: working plan (same files, mirrored) ===")
for k in sorted(plan):
    print(f"  D:\\pocket4p\\DCIM\\{k}  <- {plan[k][0]} files")
print("=== JPG+RAW pair completeness on camera ===")
comp = inc = 0
for (d, b), exts in sorted(pairs.items()):
    if "JPG" in exts and "DNG" in exts: comp += 1
    elif "JPG" in exts or "DNG" in exts:
        inc += 1; print("  INCOMPLETE", b, dict(exts))
print(f"  complete pairs: {comp}   incomplete: {inc}")
if skipped: print("  unmatched names (would go to OTHER):", skipped[:5])
