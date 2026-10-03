#!/usr/bin/env python3

import argparse
import hmac
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


MAX_FILE_SIZE = 100 * 1024 * 1024
MAX_CLIPBOARD_SIZE = 1024 * 1024
CLIPBOARD_LEASE_SECONDS = 5
MAX_QUEUED_FILES = 20
MAX_QUEUED_BYTES = 500 * 1024 * 1024
MAX_PENDING_RECEIVERS = 10
PAIR_EXPIRY_SECONDS = 300
RECEIVER_EXPIRY_SECONDS = 24 * 60 * 60
CLIENT_SCRIPT = Path(__file__).with_name("file_transfer_client.py")


def save_state(path, token, receiver_tokens):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    state = {
        "token": token,
        "receiver_tokens": list(receiver_tokens),
    }
    fd, temporary_path = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file)
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary_path, path)
    except Exception:
        Path(temporary_path).unlink(missing_ok=True)
        raise


def load_state(path, token_override=None):
    if path.exists():
        path.chmod(0o600)
        state = json.loads(path.read_text(encoding="utf-8"))
        token = token_override or state["token"]
        receiver_tokens = {
            value: None for value in state.get("receiver_tokens", [])
            if isinstance(value, str) and value
        }
    else:
        token = token_override or secrets.token_urlsafe(32)
        receiver_tokens = {}
    save_state(path, token, receiver_tokens)
    return token, receiver_tokens


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

    def _authorized_receiver(self):
        scheme, _, token = self.headers.get("Authorization", "").partition(" ")
        with self.server.pair_lock:
            expires = self.server.receiver_tokens.get(token)
            authorized = (scheme.lower() == "bearer" and token in self.server.receiver_tokens and
                          (expires is None or expires > self.server.clock()))
            if expires is not None and expires <= self.server.clock():
                del self.server.receiver_tokens[token]
        if not authorized:
            self._json(401, {"error": "Receiver authorization required"})
        return authorized

    def _authorized_transfer(self):
        scheme, _, token = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(token, self.server.token):
            return "admin"
        if self._authorized_receiver():
            return "receiver"
        return None

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

        if self.path.startswith("/api/pair/status/"):
            pair_id = self.path.removeprefix("/api/pair/status/")
            with self.server.pair_lock:
                pair = self.server.pending_pairs.get(pair_id)
                if pair is None:
                    self._json(404, {"error": "Pairing request expired"})
                elif pair["expires"] is not None and pair["expires"] <= self.server.clock():
                    del self.server.pending_pairs[pair_id]
                    self._json(410, {"error": "Pairing request expired"})
                elif pair["status"] == "approved":
                    self._json(200, {"token": pair["receiver_token"]})
                else:
                    self._send(202, b"")
            return

        if self.path == "/api/pair/pending":
            if not self._authorized():
                return
            now = self.server.clock()
            with self.server.pair_lock:
                expired = [key for key, pair in self.server.pending_pairs.items()
                           if pair["expires"] is not None and pair["expires"] <= now]
                for key in expired:
                    del self.server.pending_pairs[key]
                pending = [
                    {"id": key, "address": pair["address"],
                     "age": max(0, int(now - pair["created"]))}
                    for key, pair in self.server.pending_pairs.items()
                    if pair["status"] == "pending"
                ]
            self._json(200, {"requests": pending})
            return

        if self.path == "/api/next":
            if not self._authorized_transfer():
                return
            file_info = self.server.file_queue.next_file()
            if file_info is None:
                self._send(204, b"")
            else:
                self._json(200, file_info)
            return

        clipboard_path, _, clipboard_query = self.path.partition("?")
        if clipboard_path == "/api/clipboard":
            if not self._authorized_transfer():
                return
            with self.server.clipboard_lock:
                clipboard = dict(self.server.clipboard)
            since_values = urllib.parse.parse_qs(clipboard_query).get("since")
            if since_values:
                try:
                    since = int(since_values[0])
                except ValueError:
                    self._json(400, {"error": "Invalid clipboard revision"})
                    return
                if since == clipboard["revision"]:
                    self._send(204, b"")
                    return
            self._json(200, clipboard)
            return

        if self.path == "/api/clipboard/active":
            if not self._authorized_transfer():
                return
            with self.server.clipboard_lock:
                active = self.server.clipboard_active_until > self.server.clock()
            self._json(200, {"active": active})
            return

        if self.path.startswith("/api/file/"):
            if not self._authorized_transfer():
                return
            file_id = self.path.removeprefix("/api/file/")
            item = self.server.file_queue.read(file_id)
            if item is None:
                self._json(404, {"error": "File not found"})
                return
            _, body = item
            self._send(200, body, "application/octet-stream")
            return

        if not self._authorized():
            return
        self._json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path == "/api/clipboard/active":
            if not self._authorized():
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._json(411, {"error": "A valid Content-Length is required"})
                return
            if length < 0 or length > 1024:
                self._json(413, {"error": "Invalid clipboard session payload"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"error": "Invalid clipboard session payload"})
                return
            if not isinstance(payload, dict) or not isinstance(payload.get("active"), bool):
                self._json(400, {"error": "Invalid clipboard session state"})
                return
            with self.server.clipboard_lock:
                self.server.clipboard_active_until = (
                    self.server.clock() + CLIPBOARD_LEASE_SECONDS
                    if payload["active"] else 0
                )
            self._json(200, {"active": payload["active"]})
            return

        if self.path == "/api/pair":
            now = self.server.clock()
            with self.server.pair_lock:
                expired = [key for key, pair in self.server.pending_pairs.items()
                           if pair["expires"] is not None and pair["expires"] <= now]
                for key in expired:
                    del self.server.pending_pairs[key]
                if len(self.server.pending_pairs) >= MAX_PENDING_RECEIVERS:
                    self._json(429, {"error": "Too many pairing requests"})
                    return
                pair_id = secrets.token_urlsafe(32)
                self.server.pending_pairs[pair_id] = {
                    "address": self.client_address[0],
                    "created": now,
                    "expires": now + PAIR_EXPIRY_SECONDS,
                    "receiver_token": None,
                    "status": "pending",
                }
            self._json(202, {"id": pair_id})
            return

        if self.path.startswith("/api/complete/"):
            if not self._authorized_transfer():
                return
            file_id = self.path.removeprefix("/api/complete/")
            if not self.server.file_queue.complete(file_id):
                self._json(404, {"error": "File not found"})
                return
            self._json(200, {"ok": True})
            return

        if self.path.startswith("/api/pair/approve/"):
            if not self._authorized():
                return
            pair_id = self.path.removeprefix("/api/pair/approve/")
            with self.server.pair_lock:
                pair = self.server.pending_pairs.get(pair_id)
                if (pair is None or
                        (pair["expires"] is not None and pair["expires"] <= self.server.clock())):
                    if pair is not None:
                        del self.server.pending_pairs[pair_id]
                    self._json(404, {"error": "Pairing request expired"})
                    return
                if pair["status"] != "pending":
                    self._json(409, {"error": "Pairing request already approved"})
                    return
                pair["receiver_token"] = secrets.token_urlsafe(32)
                pair["status"] = "approved"
                pair["expires"] = None
                self.server.receiver_tokens[pair["receiver_token"]] = None
                save_state(self.server.state_file, self.server.token,
                           self.server.receiver_tokens)
            self._json(200, {"ok": True})
            return

        if self.path == "/api/clipboard":
            role = self._authorized_transfer()
            if not role:
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._json(411, {"error": "A valid Content-Length is required"})
                return
            if length < 0 or length > MAX_CLIPBOARD_SIZE:
                self._json(413, {"error": "Clipboard text exceeds the 1 MiB limit"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"error": "Invalid clipboard payload"})
                return
            expected_source = "browser" if role == "admin" else "linux"
            if (not isinstance(payload, dict) or payload.get("source") != expected_source or
                    not isinstance(payload.get("text"), str)):
                self._json(400, {"error": "Invalid clipboard source or text"})
                return
            text = payload["text"]
            if len(text.encode("utf-8")) > MAX_CLIPBOARD_SIZE:
                self._json(413, {"error": "Clipboard text exceeds the 1 MiB limit"})
                return
            with self.server.clipboard_lock:
                if text != self.server.clipboard["text"]:
                    self.server.clipboard = {
                        "revision": self.server.clipboard["revision"] + 1,
                        "source": expected_source,
                        "text": text,
                    }
                clipboard = dict(self.server.clipboard)
            self._json(200, clipboard)
            return

        if not self._authorized():
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
    parser.add_argument(
        "--state-file",
        default=str(Path.home() / ".local/state/novnc-file-relay/state.json"),
        help="persistent token state file",
    )
    args = parser.parse_args()

    state_file = Path(args.state_file).expanduser()
    token, receiver_tokens = load_state(state_file, args.token)
    file_queue = FileQueue()
    server = ThreadingHTTPServer((args.host, args.port), RelayHandler)
    server.token = token
    server.file_queue = file_queue
    server.allowed_origin = args.origin
    server.clock = time.time
    server.pair_lock = threading.Lock()
    server.pending_pairs = {}
    server.receiver_tokens = receiver_tokens
    server.state_file = state_file
    server.clipboard_lock = threading.Lock()
    server.clipboard = {"revision": 0, "source": None, "text": ""}
    server.clipboard_active_until = 0

    print(f"File relay listening on {args.host}:{args.port}")
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
