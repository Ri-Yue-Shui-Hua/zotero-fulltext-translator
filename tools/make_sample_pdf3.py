#!/usr/bin/env python3
"""Body-text stress sample:
 * left column: justified paragraph drawn word-by-word (one stretched 3-word line with huge gaps)
 * right column: normal paragraph + an oversized 3-line paragraph (anomalous size) + an invisible duplicate layer
 * a heading and a real table block (must stay split into cells)"""
import sys, pymupdf as fitz
out = sys.argv[1] if len(sys.argv) > 1 else "sample3.pdf"
doc = fitz.open(); pg = doc.new_page(width=595, height=842)
ft = fitz.Font("tiro"); fs = 9.5
def justified(words_per_line, x0, x1, y, lh=12.0):
    """Every word is its own text object; some words are italic (a different font => different PDF span)."""
    fi = fitz.Font("tiit")
    for n, line in enumerate(words_per_line):
        fonts = [(fi, "tiit") if (i % 2 == 1 and n == 2) else (ft, "tiro") for i in range(len(line))]
        total = sum(f.text_length(w + " ", fontsize=fs) for w, (f, _) in zip(line, fonts))
        gap = (x1 - x0 - total) / max(len(line) - 1, 1) + ft.text_length(" ", fontsize=fs)
        x = x0
        for w, (f, name) in zip(line, fonts):
            pg.insert_text((x, y), w, fontsize=fs, fontname=name)
            x += f.text_length(w, fontsize=fs) + gap
        y += lh
    return y
L = [["All","examinations","were","performed","by","one"],
     ["radiologist","with","nine","years","and","five","years"],
     ["Siemens","(Siemens Healthcare, Germany)","unit"],   # stretched 3-fragment line (italic words => separate spans)
     ["with","virtual","touch","tissue","imaging","(VTIQ)","for"],
     ["SWE","based","on","ARFI","imaging."]]
justified(L, 60, 290, 90)
# right column: normal paragraph, then oversized paragraph, then invisible duplicate of the first one
p1 = ("The musculoskeletal preset was used for examination. The probe was placed on the knee in a transverse plane, "
      "and the patient was asked to flex the knee to 90 degrees so that the femoral cartilage was exposed.")
p2 = ("Measurements were obtained at three independent sites, including the medial femoral condyle, the intercondylar "
      "region and the lateral femoral condyle, and each measurement was repeated three times by one operator.")
pg.insert_textbox(fitz.Rect(310, 90, 535, 190), p1, fontsize=9.5, fontname="tiro")
pg.insert_textbox(fitz.Rect(310, 200, 535, 330), p2, fontsize=14, fontname="tiro")   # anomalous size
tw = fitz.TextWriter(pg.rect, color=(0, 0, 0))                                         # invisible bigger duplicate of p1
words = p1.split(); x, y = 310, 100
for w in words:
    wd = ft.text_length(w + " ", fontsize=12)
    if x + wd > 535: x = 310; y += 14
    tw.append((x, y), w, font=ft, fontsize=12); x += wd
tw.write_text(pg, render_mode=3)
pg.insert_textbox(fitz.Rect(60, 40, 535, 70), "Materials and Methods", fontsize=14, fontname="hebo")
# table block
ty = 420
pg.insert_text((270, ty), "Study group", fontsize=9, fontname="helv"); pg.insert_text((420, ty), "Control group", fontsize=9, fontname="helv")
for i, r in enumerate([("Medial cartilage thickness (mm)", "1.77 ± 0.47", "1.00-2.80", "1.80 ± 0.28", "1.30-2.50", "0.711"),
                       ("Lateral cartilage thickness (mm)", "1.71 ± 0.42", "0.90-2.70", "1.66 ± 0.32", "0.90-2.40", "0.575")]):
    tw = fitz.TextWriter(pg.rect); y = ty + 20 + i * 16
    for x, t in zip([60, 270, 340, 420, 490, 540], r): tw.append((x, y), t, font=ft, fontsize=9)
    tw.write_text(pg)
doc.save(out); print("wrote", out)
