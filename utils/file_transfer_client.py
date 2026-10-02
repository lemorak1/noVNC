#!/usr/bin/env python3

import argparse
import getpass
import json
import os
import pathlib
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


def request(base_url, token, path):
    req = urllib.request.Request(
        base_url + path,
        headers={"Authorization": "Bearer " + token},
    )
    return urllib.request.urlopen(req, timeout=60)


def unique_path(directory, filename):
    name = filename.replace("\\", "/").split("/")[-1].replace("\0", "")
    if name in {"", ".", ".."}:
        name = "uploaded-file"
    candidate = directory / name
    if not candidate.exists():
        return candidate

    path = pathlib.Path(name)
    index = 1
    while True:
        candidate = directory / f"{path.stem} ({index}){path.suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def receive_one(base_url, token, directory):
    with request(base_url, token, "/api/next") as response:
        if response.status == 204:
            return False
        info = json.load(response)

    file_id = info.get("id", "")
    if not file_id.isalnum():
        raise ValueError("Relay returned an invalid file ID")

    with request(base_url, token, "/api/file/" + file_id) as response:
        content = response.read()

    directory.mkdir(parents=True, exist_ok=True)
    destination = unique_path(directory, info.get("filename", "uploaded-file"))
    fd, temporary_name = tempfile.mkstemp(prefix=".novnc-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        while True:
            destination = unique_path(directory, info.get("filename", "uploaded-file"))
            try:
                os.link(temporary_name, destination)
                break
            except FileExistsError:
                continue
        os.unlink(temporary_name)
    except Exception:
        pathlib.Path(temporary_name).unlink(missing_ok=True)
        raise

    req = urllib.request.Request(
        base_url + "/api/complete/" + file_id,
        data=b"",
        method="POST",
        headers={"Authorization": "Bearer " + token},
    )
    with urllib.request.urlopen(req, timeout=60):
        pass
    print(f"Saved: {destination}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Receive noVNC file uploads over HTTPS")
    parser.add_argument("--server", required=True, help="HTTPS base URL of the relay")
    parser.add_argument("--token", help="relay bearer token; prompted if omitted")
    parser.add_argument("--directory", default=str(pathlib.Path.home() / "Downloads"),
                        help="destination directory (default: ~/Downloads)")
    args = parser.parse_args()

    parsed = urllib.parse.urlparse(args.server)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
        parser.error("--server must be an HTTPS origin, e.g. https://host.example")
    base_url = args.server.rstrip("/")
    token = args.token or getpass.getpass("Relay token: ")
    if not token:
        parser.error("a relay token is required")
    directory = pathlib.Path(args.directory).expanduser()
    print(f"Waiting for uploads into {directory}")
    try:
        while True:
            try:
                if not receive_one(base_url, token, directory):
                    time.sleep(2)
            except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as err:
                print(f"Transfer error: {err}", file=sys.stderr)
                time.sleep(5)
    except KeyboardInterrupt:
        print("\nStopping file receiver")


if __name__ == "__main__":
    main()
