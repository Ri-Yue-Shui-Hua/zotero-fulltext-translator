#!/usr/bin/env python3
"""Count overlapping text lines in a PDF page and report missing numbers. Usage: check_overlap.py out.pdf [number ...]"""
import sys, pymupdf as fitz
doc = fitz.open(sys.argv[1]); bad = 0; total = 0
for pg in doc:
    lines = []
    for b in pg.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            if "".join(s["text"] for s in l["spans"]).strip(): lines.append(fitz.Rect(l["bbox"]))
    total += len(lines)
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            inter = lines[i] & lines[j]
            if inter.is_empty: continue
            a = inter.width * inter.height; m = min(lines[i].width * lines[i].height, lines[j].width * lines[j].height)
            if m > 0 and a / m > 0.3: bad += 1
txt = "".join(p.get_text() for p in doc)
missing = [n for n in sys.argv[2:] if n not in txt]
print(f"lines={total} overlapping_pairs={bad} missing_numbers={missing}")
# --- font size uniformity of CJK body lines (size histogram) ---
from collections import Counter
c = Counter()
for pg in doc:
    for b in pg.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            t = "".join(s["text"] for s in l["spans"])
            if sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff") >= 6:
                c[round(max(s["size"] for s in l["spans"]), 1)] += 1
print("CJK line font sizes:", dict(sorted(c.items())))
