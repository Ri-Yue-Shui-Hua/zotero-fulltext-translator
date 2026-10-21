#!/usr/bin/env python3
"""Stress sample: dense consecutive paragraphs (no gaps) + a table with numeric columns."""
import sys, pymupdf as fitz
out = sys.argv[1] if len(sys.argv) > 1 else "sample2.pdf"
doc = fitz.open(); pg = doc.new_page(width=595, height=842)
paras = [
 "All examinations were performed by one radiologist with nine years and five years of experience in ultrasound and SWE, respectively. A branded device equipped with virtual touch tissue imaging quantification (VTIQ) was used for SWE based on ARFI imaging, with a 9L4 linear probe at 4-9 MHz.",
 "The musculoskeletal preset was used for examination. The probe was placed on the knee in a transverse plane, and the patient was asked to flex the knee to 90 degrees so that the femoral cartilage was exposed above the patella.",
 "Measurements were obtained at three independent sites, including the medial femoral condyle, the intercondylar region and the lateral femoral condyle, and each measurement was repeated three times by the same operator.",
 "The region of interest was set to 2 mm in diameter and was placed in the middle of the cartilage layer. Values that were affected by artifacts were excluded from the final analysis of the study cohort.",
]
# all paragraphs in ONE text block (like many journal PDFs) -> several paragraphs per PyMuPDF block
pg.insert_textbox(fitz.Rect(60, 60, 535, 400), "\n".join(paras), fontsize=9.5, fontname="tiro")
ty = 420
pg.insert_text((270, ty), "Study group", fontsize=9, fontname="helv")
pg.insert_text((420, ty), "Control group", fontsize=9, fontname="helv")
ft = fitz.Font("tiro")
ROWS = [("Medial cartilage thickness (mm)", "0.711 ± 0.12", "0.50-0.93", "0.700 ± 0.10", "0.48-0.90", "0.575"),
        ("Intercondylar cartilage thickness (mm)", "0.766 ± 0.15", "0.52-1.02", "0.741 ± 0.13", "0.50-0.99", "0.002"),
        ("Lateral cartilage shear wave velocity (m/s)", "0.652 ± 0.11", "0.44-0.85", "0.640 ± 0.09", "0.43-0.82", "0.003")]
for i, r in enumerate(ROWS):
    y = ty + 20 + i * 16
    tw = fitz.TextWriter(pg.rect)  # one text object per row, columns far apart -> cells share a PDF line/block
    for x, t in zip([60, 270, 340, 420, 490, 540], r):
        tw.append((x, y), t, font=ft, fontsize=9)
    tw.write_text(pg)
pg.insert_text((60, ty + 80), "SWV, shear wave velocity.", fontsize=8, fontname="tiro")
doc.save(out); print("wrote", out)
