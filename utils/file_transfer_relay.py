#!/usr/bin/env python3

import argparse
import hmac
import json
import secrets
import sys
import tempfile
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


MAX_FILE_SIZE = 100 * 1024 * 1024
MAX_QUEUED_FILES = 20
MAX_QUEUED_BYTES = 500 * 1024 * 1024
CLIENT_SCRIPT = Path(__file__).with_name("file_transfer_client.py")


class FileQueue:
    def __init__(self):
        self._directory = tempfile.TemporaryDirectory(prefix="novnc-upload-")
        self._files = {}
        self._lock = threading.Lock()

    def add(self, name, content):
        with self._lock:
            queued_size = sum(item["size"] for item in self._files.values())
            if len(self._files) >= MAX_QUEUED_FILES:
                raise OverflowError("Too many files are waiting for download")
            if queued_size + len(content) > MAX_QUEUED_BYTES:
                raise OverflowError("The file queue is full")

            file_id = uuid.uuid4().hex
            path = Path(self._directory.name) / file_id
            path.write_bytes(content)
            self._files[file_id] = {
                "name": name,
                "path": path,
                "size": len(content),
            }
            return file_id

    def next_file(self):
        with self._lock:
            if not self._files:
                return None
            file_id = next(iter(self._files))
            item = self._files[file_id]
            return {
                "id": file_id,
                "filename": item["name"],
                "size": item["size"],
            }

    def read(self, file_id):
        with self._lock:
            item = self._files.get(file_id)
            if item is None:
                return None
            return item["name"], item["path"].read_bytes()

    def complete(self, file_id):
        with self._lock:
            item = self._files.pop(file_id, None)
            if item is None:
                return False
            item["path"].unlink(missing_ok=True)
            return True

    def close(self):
        self._directory.cleanup()


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "noVNCFileRelay/1.0"

    def log_message(self, format_string, *args):
        print("%s - %s" % (self.client_address[0], format_string % args))

    def _cors_headers(self):
        origin = self.headers.get("Origin")
        allowed_origin = self.server.allowed_origin
        if allowed_origin == "*" or origin == allowed_origin:
            self.send_header("Access-Control-Allow-Origin", allowed_origin if allowed_origin != "*" else "*")
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-File-Name")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Max-Age", "600")

    def _send(self, status, body=b"", content_type="application/json"):
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status, value):
        body = json.dumps(value).encode("utf-8")
        self._send(status, body)

    def _authorized(self):
        scheme, _, token = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token, self.server.token):
            self._json(401, {"error": "Unauthorized"})
            return False
        return True

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/client":
            try:
                body = CLIENT_SCRIPT.read_bytes()
            except OSError as err:
                self._json(500, {"error": "Could not read transfer client: " + str(err)})
                return
            self._send(200, body, "text/x-python; charset=utf-8")
            return

        if not self._authorized():
            return

        if self.path == "/api/next":
            file_info = self.server.file_queue.next_file()
            if file_info is None:
                self._send(204, b"")
            else:
                self._json(200, file_info)
            return

        if self.path.startswith("/api/file/"):
            file_id = self.path.removeprefix("/api/file/")
            item = self.server.file_queue.read(file_id)
            if item is None:
                self._json(404, {"error": "File not found"})
                return
            _, body = item
            self._send(200, body, "application/octet-stream")
            return

        self._json(404, {"error": "Not found"})

    def do_POST(self):
        if not self._authorized():
            return

        if self.path.startswith("/api/complete/"):
            file_id = self.path.removeprefix("/api/complete/")
            if not self.server.file_queue.complete(file_id):
                self._json(404, {"error": "File not found"})
                return
            self._json(200, {"ok": True})
            return

        if self.path != "/api/upload":
            self._json(404, {"error": "Not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json(411, {"error": "A valid Content-Length is required"})
            return
        if length < 0 or length > MAX_FILE_SIZE:
            self._json(413, {"error": "File exceeds the 100 MiB limit"})
            return

        encoded_name = self.headers.get("X-File-Name", "")
        try:
            name = urllib.parse.unquote(encoded_name, encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            self._json(400, {"error": "Invalid filename encoding"})
            return
        if (not name or name in {".", ".."} or "/" in name or "\\" in name or
                any(ord(char) < 32 for char in name)):
            self._json(400, {"error": "Invalid filename"})
            return

        remaining = length
        chunks = []
        while remaining:
            chunk = self.rfile.read(min(64 * 1024, remaining))
            if not chunk:
                self._json(400, {"error": "Incomplete upload"})
                return
            chunks.append(chunk)
            remaining -= len(chunk)

        try:
            file_id = self.server.file_queue.add(name, b"".join(chunks))
        except OverflowError as err:
            self._json(429, {"error": str(err)})
            return

        self._json(202, {"id": file_id, "filename": name})


def main():
    parser = argparse.ArgumentParser(description="HTTPS-forwarded noVNC file transfer relay")
    parser.add_argument("--host", default="0.0.0.0", help="listen address (default: all interfaces)")
    parser.add_argument("--port", type=int, default=8765, help="listen port (default: 8765)")
    parser.add_argument("--token", help="bearer token; generated if omitted")
    parser.add_argument("--origin", default="*", help="allowed noVNC page origin (default: *)")
    args = parser.parse_args()

    token = args.token or secrets.token_urlsafe(32)
    file_queue = FileQueue()
    server = ThreadingHTTPServer((args.host, args.port), RelayHandler)
    server.token = token
    server.file_queue = file_queue
    server.allowed_origin = args.origin

    print(f"File relay listening on {args.host}:{args.port}")
    sys.stdout.write("Relay access token (copy this): ")
    sys.stdout.flush()
    print(token)
    print(f"Bearer token: {token}")
    print("Use the HTTPS forwarded URL for this port in noVNC and the Linux client.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping file relay")
    finally:
        server.server_close()
        file_queue.close()


if __name__ == "__main__":
    main()
