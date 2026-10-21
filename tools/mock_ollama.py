#!/usr/bin/env python3
"""A tiny fake Ollama server for testing the engine without a GPU/model.
Usage: python tools/mock_ollama.py [port]"""
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def _send(self, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/api/version": self._send({"version": "mock"})
        elif self.path == "/api/tags": self._send({"models": [{"name": "qwen3.5:4b"}]})
        else: self.send_error(404)
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); req = json.loads(self.rfile.read(n))
        user = req["messages"][-1]["content"]
        if "format" in req:
            payload = json.loads(user[user.index("\n\n[") + 2:])
            def tr(t):
                import re
                phs = " ".join(re.findall(r"\[\[\d+\]\]", t))
                n = max(2, int(0.6 * len(re.sub(r"\[\[\d+\]\]", "", t))))
                return (("这是对原文的中文翻译内容用于测试排版" * 40)[:n] + " " + phs).strip()
            out = {"translations": [{"id": p["id"], "text": tr(p["text"])} for p in payload]}
            self._send({"message": {"content": json.dumps(out, ensure_ascii=False)}})
        else:
            self._send({"message": {"content": "【译】" + user[:60] + "的中文翻译（单条回退）。"}})
    def log_message(self, *a): pass

HTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 11999), H).serve_forever()
