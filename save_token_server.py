#!/usr/bin/env python3
import http.server
import json
import os
from pathlib import Path


TOKEN_FILE = Path(os.environ.get("HA_TOKEN_FILE", "/Users/max/.hermes/secrets/ha_token.txt"))

class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers['Content-Length'])
        body = json.loads(self.rfile.read(length))
        token = body.get('token', '')
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(token + '\n', encoding='utf-8')
        os.chmod(TOKEN_FILE, 0o600)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "len": len(token)}).encode())
        print(f"Token saved, length: {len(token)}")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def log_message(self, format, *args):
        print(format % args)

server = http.server.HTTPServer(('127.0.0.1', 19876), Handler)
print("Token server ready on :19876")
server.handle_request()
server.handle_request()
print("Done")
