#!/usr/bin/env python3
"""Generate a 2-page two-column sample paper PDF for testing."""
import sys, pymupdf as fitz
out = sys.argv[1] if len(sys.argv) > 1 else "sample.pdf"
doc = fitz.open(); page = doc.new_page(width=595, height=842)
page.insert_textbox(fitz.Rect(60, 50, 535, 110), "Attention Is All You Need: A Sample Paper on Sequence Transduction",
                    fontsize=18, fontname="hebo", align=1)
page.insert_textbox(fitz.Rect(60, 112, 535, 130), "Alice Smith, Bob Jones", fontsize=11, fontname="helv", align=1)
para = ("The dominant sequence transduction models are based on complex recurrent or convolutional neural networks "
        "that include an encoder and a decoder. The best performing models also connect the encoder and decoder "
        "through an attention mechanism, as described by Vaswani et al. [12]. We propose a new simple network "
        "architecture, the Transformer, based solely on attention mechanisms.")
page.insert_textbox(fitz.Rect(60, 150, 290, 330), "1 Introduction", fontsize=13, fontname="hebo")
page.insert_textbox(fitz.Rect(60, 175, 290, 400), para, fontsize=9.5, fontname="tiro")
page.insert_textbox(fitz.Rect(305, 175, 535, 400), para + " Experiments on two machine translation tasks show these models to be superior in quality.", fontsize=9.5, fontname="tiro")
page.draw_rect(fitz.Rect(60, 420, 535, 520), color=(0.2, 0.4, 0.8), fill=(0.9, 0.95, 1))
page.insert_textbox(fitz.Rect(66, 425, 530, 515), "Figure 1: The Transformer model architecture, with stacked self-attention and point-wise layers for both the encoder and decoder.", fontsize=9, fontname="tiit")
page.insert_text((60, 560), "Page 1", fontsize=8)
p2 = doc.new_page(width=595, height=842)
p2.insert_textbox(fitz.Rect(60, 60, 535, 90), "References", fontsize=13, fontname="hebo")
p2.insert_textbox(fitz.Rect(60, 95, 535, 200), "[1] Vaswani, A., Shazeer, N. Attention is all you need. Advances in Neural Information Processing Systems, 2017.", fontsize=9)
doc.save(out); print("wrote", out)
