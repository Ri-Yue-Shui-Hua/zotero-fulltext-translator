#!/usr/bin/env python3
"""Pack the plugin into dist/<name>-<version>.xpi (a zip with manifest.json at the root)."""
import json, os, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ["manifest.json", "bootstrap.js", "plugin.js", "prefs.js", "prefs.xhtml", "prefs-pane.js",
         "engine/translate_pdf.py"]
ver = json.load(open(os.path.join(ROOT, "manifest.json"), encoding="utf-8"))["version"]
os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
out = os.path.join(ROOT, "dist", "zotero-fulltext-translator-%s.xpi" % ver)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for f in FILES:
        z.write(os.path.join(ROOT, f), f)  # arcname uses forward slashes
print("built", out, os.path.getsize(out), "bytes")
