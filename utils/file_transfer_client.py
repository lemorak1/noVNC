#!/usr/bin/env python3

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


CLIPBOARD_MAX_BYTES = 1024 * 1024


class ClipboardUnavailable(RuntimeError):
    pass


def request(base_url, path, token=None, method="GET", body=None):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        base_url + path, data=body, method=method, headers=headers,
    )
    return urllib.request.urlopen(req, timeout=60)


def read_desktop_clipboard():
    if not os.environ.get("DISPLAY"):
        raise ClipboardUnavailable("DISPLAY is not set; run the receiver in the Ubuntu desktop session")
    try:
        result = subprocess.run(
            ["xclip", "-selection", "clipboard", "-o"],
            check=True, capture_output=True, timeout=3,
        )
    except FileNotFoundError as err:
        raise ClipboardUnavailable("xclip is required (install it with: sudo apt install xclip)") from err
    except subprocess.CalledProcessError:
        return ""
    except subprocess.TimeoutExpired as err:
        raise ClipboardUnavailable("Timed out reading the Ubuntu clipboard") from err
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise ClipboardUnavailable("Ubuntu clipboard does not contain UTF-8 text")
    if len(text.encode("utf-8")) > CLIPBOARD_MAX_BYTES:
        raise ClipboardUnavailable("Ubuntu clipboard text exceeds the 1 MiB limit")
    return text


def write_desktop_clipboard(text):
    if not os.environ.get("DISPLAY"):
        raise ClipboardUnavailable("DISPLAY is not set; run the receiver in the Ubuntu desktop session")
    try:
        subprocess.run(
            ["xclip", "-selection", "clipboard", "-i"],
            input=text.encode("utf-8"), check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
        )
    except FileNotFoundError as err:
        raise ClipboardUnavailable("xclip is required (install it with: sudo apt install xclip)") from err
    except subprocess.CalledProcessError as err:
        raise ClipboardUnavailable("Could not write to the Ubuntu clipboard") from err
    except subprocess.TimeoutExpired as err:
        raise ClipboardUnavailable("Timed out writing the Ubuntu clipboard") from err


def sync_clipboard_once(base_url, token, last_local_text, last_revision):
    with request(base_url, "/api/clipboard/active", token=token) as response:
        if not json.load(response)["active"]:
            return last_local_text, last_revision

    local_text = read_desktop_clipboard()
    with request(base_url, f"/api/clipboard?since={last_revision}", token=token) as response:
        if response.status == 204:
            state = {"revision": last_revision, "source": None, "text": None}
        else:
            state = json.load(response)

    if (state["source"] == "browser" and state["revision"] > last_revision and
            state["text"] != local_text):
        write_desktop_clipboard(state["text"])
        local_text = state["text"]
    elif local_text != last_local_text and local_text != state["text"]:
        payload = json.dumps({"source": "linux", "text": local_text}).encode("utf-8")
        with request(base_url, "/api/clipboard", token=token,
                     method="POST", body=payload) as response:
            state = json.load(response)

    return local_text, state["revision"]


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


def pair_receiver(base_url):
    with request(base_url, "/api/pair", method="POST", body=b"") as response:
        pair_id = json.load(response)["id"]
    print("Pairing requested. Approve the receiver from noVNC Settings.")
    while True:
        with request(base_url, "/api/pair/status/" + pair_id) as response:
            if response.status == 200:
                token = json.load(response).get("token")
                if not token:
                    raise ValueError("Relay returned an invalid receiver token")
                return token
        time.sleep(2)


def load_token(path):
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not token:
        path.unlink(missing_ok=True)
        return None
    path.chmod(0o600)
    return token


def save_token(path, token):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temporary_path = tempfile.mkstemp(prefix=".token-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as token_file:
            token_file.write(token + "\n")
            token_file.flush()
            os.fsync(token_file.fileno())
        os.replace(temporary_path, path)
    except Exception:
        pathlib.Path(temporary_path).unlink(missing_ok=True)
        raise


def install_desktop_session_hook(home):
    helper = home / ".local/bin/novnc-file-transfer-session-start"
    helper.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    helper.parent.chmod(0o700)
    helper.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "environment=()\n"
        '[[ -n "${DISPLAY:-}" ]] && environment+=(DISPLAY)\n'
        '[[ -n "${XAUTHORITY:-}" ]] && environment+=(XAUTHORITY)\n'
        'if ((${#environment[@]})); then\n'
        '    systemctl --user import-environment "${environment[@]}"\n'
        "fi\n"
        "systemctl --user restart novnc-file-transfer.service\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)

    autostart_directory = home / ".config/autostart"
    autostart_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    autostart_directory.chmod(0o700)
    desktop_exec = '"' + str(helper).replace("\\", "\\\\").replace('"', '\\"') + '"'
    autostart = autostart_directory / "novnc-file-transfer-session.desktop"
    autostart.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=noVNC file transfer session setup\n"
        f"Exec={desktop_exec}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )
    autostart.chmod(0o600)


def install_user_service(server, directory, token_file):
    script = pathlib.Path(__file__).resolve()
    home = pathlib.Path.home()
    unit_directory = home / ".config/systemd/user"
    unit_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    unit_path = unit_directory / "novnc-file-transfer.service"

    args = [
        sys.executable, str(script), "--server", server,
        "--directory", str(directory), "--token-file", str(token_file),
    ]
    command = " ".join(
        '"' + arg.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'
        for arg in args
    )
    unit_path.write_text(
        "[Unit]\n"
        "Description=noVNC file transfer receiver\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={command}\n"
        "Restart=always\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n",
        encoding="utf-8",
    )
    unit_path.chmod(0o600)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    environment = [name for name in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY")
                   if name in os.environ]
    if environment:
        subprocess.run(
            ["systemctl", "--user", "import-environment", *environment], check=True,
        )
    subprocess.run(
        ["systemctl", "--user", "enable", "novnc-file-transfer.service"],
        check=True,
    )
    subprocess.run(
        ["systemctl", "--user", "restart", "novnc-file-transfer.service"],
        check=True,
    )
    install_desktop_session_hook(home)
    print("Installed and started the file and clipboard receiver as a user service.")
    print("Ubuntu desktop login will refresh DISPLAY/XAUTHORITY and restart the receiver.")


def receive_one(base_url, token, directory):
    with request(base_url, "/api/next", token=token) as response:
        if response.status == 204:
            return False
        info = json.load(response)

    file_id = info.get("id", "")
    if not file_id.isalnum():
        raise ValueError("Relay returned an invalid file ID")

    with request(base_url, "/api/file/" + file_id, token=token) as response:
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
        base_url + "/api/complete/" + file_id, data=b"", method="POST",
        headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=60):
        pass
    print(f"Saved: {destination}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Receive noVNC file uploads over HTTPS")
    parser.add_argument("--server", required=True, help="HTTPS base URL of the relay")
    parser.add_argument("--directory", default=str(pathlib.Path.home() / "Downloads"),
                        help="destination directory (default: ~/Downloads)")
    parser.add_argument(
        "--token-file",
        default=str(pathlib.Path.home() / ".config/novnc-file-transfer/token"),
        help="protected file for the saved receiver identity",
    )
    parser.add_argument("--install-user-service", action="store_true",
                        help="install and start a systemd user service")
    args = parser.parse_args()

    parsed = urllib.parse.urlparse(args.server)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
        parser.error("--server must be an HTTPS origin, e.g. https://host.example")
    base_url = args.server.rstrip("/")
    directory = pathlib.Path(args.directory).expanduser()
    token_file = pathlib.Path(args.token_file).expanduser()
    if args.install_user_service:
        install_user_service(base_url, directory, token_file)
        return

    print(f"Waiting for uploads into {directory} and clipboard text")
    try:
        token = load_token(token_file)
        if token:
            print("Using saved receiver identity.")
        else:
            token = pair_receiver(base_url)
            save_token(token_file, token)
            print("Receiver paired and identity saved.")
        print("Leave this terminal running to receive files.")
        local_clipboard = None
        clipboard_revision = 0
        next_file_poll = 0
        next_clipboard_poll = 0
        clipboard_error_reported = False
        while True:
            try:
                now = time.monotonic()
                if now >= next_clipboard_poll:
                    try:
                        local_clipboard, clipboard_revision = sync_clipboard_once(
                            base_url, token, local_clipboard, clipboard_revision,
                        )
                        next_clipboard_poll = now + 1
                        clipboard_error_reported = False
                    except ClipboardUnavailable as err:
                        if not clipboard_error_reported:
                            print(f"Clipboard sync unavailable: {err}", file=sys.stderr)
                            clipboard_error_reported = True
                        next_clipboard_poll = now + 10
                if now >= next_file_poll:
                    receive_one(base_url, token, directory)
                    next_file_poll = now + 2
                time.sleep(0.5)
            except urllib.error.HTTPError as err:
                if err.code == 401:
                    err.close()
                    token_file.unlink(missing_ok=True)
                    print("Saved receiver identity was revoked; requesting approval again.")
                    token = pair_receiver(base_url)
                    save_token(token_file, token)
                    continue
                print(f"Transfer error: {err}", file=sys.stderr)
                time.sleep(5)
            except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as err:
                print(f"Transfer error: {err}", file=sys.stderr)
                time.sleep(5)
    except KeyboardInterrupt:
        print("\nStopping file receiver")


if __name__ == "__main__":
    main()
