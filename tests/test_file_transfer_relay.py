import importlib.util
import json
import pathlib
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock


RELAY_PATH = pathlib.Path(__file__).parents[1] / "utils" / "file_transfer_relay.py"
SPEC = importlib.util.spec_from_file_location("file_transfer_relay", RELAY_PATH)
relay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relay)
CLIENT_PATH = pathlib.Path(__file__).parents[1] / "utils" / "file_transfer_client.py"
CLIENT_SPEC = importlib.util.spec_from_file_location("file_transfer_client", CLIENT_PATH)
client = importlib.util.module_from_spec(CLIENT_SPEC)
CLIENT_SPEC.loader.exec_module(client)


class FileTransferRelayTest(unittest.TestCase):
    def setUp(self):
        self.state_directory = tempfile.TemporaryDirectory()
        self.state_file = pathlib.Path(self.state_directory.name) / "relay-state.json"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), relay.RelayHandler)
        self.server.token = "test-token"
        self.server.file_queue = relay.FileQueue()
        self.server.allowed_origin = "*"
        self.server.clock = time.time
        self.server.pair_lock = threading.Lock()
        self.server.pending_pairs = {}
        self.server.receiver_tokens = {}
        self.server.state_file = self.state_file
        self.server.clipboard_lock = threading.Lock()
        self.server.clipboard = {"revision": 0, "source": None, "text": ""}
        self.server.clipboard_active_until = 0
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.server.file_queue.close()
        self.state_directory.cleanup()

    def request(self, path, method="GET", body=None, headers=None):
        req = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method,
            headers=headers or {},
        )
        return urllib.request.urlopen(req)

    def test_upload_download_and_acknowledge(self):
        headers = {
            "Authorization": "Bearer test-token",
            "Content-Length": "7",
            "Content-Type": "application/octet-stream",
            "X-File-Name": "hello%20world.txt",
        }
        with self.request("/api/upload", "POST", b"content", headers) as response:
            self.assertEqual(response.status, 202)
            uploaded = json.load(response)

        with self.request("/api/next", headers={"Authorization": "Bearer test-token"}) as response:
            queued = json.load(response)
        self.assertEqual(queued["id"], uploaded["id"])
        self.assertEqual(queued["filename"], "hello world.txt")

        with self.request(
            "/api/file/" + uploaded["id"],
            headers={"Authorization": "Bearer test-token"},
        ) as response:
            self.assertEqual(response.read(), b"content")

        with self.request(
            "/api/complete/" + uploaded["id"],
            "POST",
            b"",
            {"Authorization": "Bearer test-token"},
        ) as response:
            self.assertEqual(response.status, 200)

        with self.request(
            "/api/next", headers={"Authorization": "Bearer test-token"}
        ) as response:
            self.assertEqual(response.status, 204)

    def test_upload_requires_token(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(
                "/api/upload",
                "POST",
                b"content",
                {"Content-Length": "7", "X-File-Name": "file.txt"},
            )
        error.exception.close()
        self.assertEqual(error.exception.code, 401)

    def test_upload_rejects_path_in_filename(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(
                "/api/upload",
                "POST",
                b"content",
                {
                    "Authorization": "Bearer test-token",
                    "Content-Length": "7",
                    "X-File-Name": "%2Ftmp%2Ffile.txt",
                },
            )
        error.exception.close()
        self.assertEqual(error.exception.code, 400)

    def test_receiver_saves_and_acknowledges_queued_file(self):
        self.server.file_queue.add("received.txt", b"file contents")
        with tempfile.TemporaryDirectory() as directory:
            destination = pathlib.Path(directory)
            self.assertTrue(client.receive_one(self.base_url, "test-token", destination))
            self.assertEqual((destination / "received.txt").read_bytes(), b"file contents")
            self.assertFalse(client.receive_one(self.base_url, "test-token", destination))

    def test_pairing_requires_explicit_approval(self):
        with self.request("/api/pair", "POST", b"") as response:
            pair_id = json.load(response)["id"]

        with self.request("/api/pair/status/" + pair_id) as response:
            self.assertEqual(response.status, 202)

        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(
                "/api/pair/pending",
                headers={"Authorization": "Bearer wrong-token"},
            )
        error.exception.close()
        self.assertEqual(error.exception.code, 401)

        with self.request(
            "/api/pair/approve/" + pair_id,
            "POST",
            b"",
            {"Authorization": "Bearer test-token"},
        ):
            pass
        with self.request("/api/pair/status/" + pair_id) as response:
            receiver_token = json.load(response)["token"]
        self.assertNotEqual(receiver_token, "test-token")
        saved_token, saved_receivers = relay.load_state(self.state_file)
        self.assertEqual(saved_token, "test-token")
        self.assertIn(receiver_token, saved_receivers)

        self.server.file_queue.add("paired.txt", b"paired")
        with self.request(
            "/api/next", headers={"Authorization": "Bearer " + receiver_token}
        ) as response:
            self.assertEqual(json.load(response)["filename"], "paired.txt")

        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(
                "/api/upload",
                "POST",
                b"blocked",
                {
                    "Authorization": "Bearer " + receiver_token,
                    "Content-Length": "7",
                    "X-File-Name": "blocked.txt",
                },
            )
        error.exception.close()
        self.assertEqual(error.exception.code, 401)

    def test_relay_and_receiver_tokens_persist_with_private_permissions(self):
        relay.save_state(self.state_file, "stable-admin-token", {"stable-receiver-token": None})
        saved_token, saved_receivers = relay.load_state(self.state_file)

        self.assertEqual(saved_token, "stable-admin-token")
        self.assertIn("stable-receiver-token", saved_receivers)
        self.assertEqual(self.state_file.stat().st_mode & 0o777, 0o600)

    def test_linux_client_saves_receiver_identity(self):
        token_file = pathlib.Path(self.state_directory.name) / "client" / "token"
        client.save_token(token_file, "receiver-identity")

        self.assertEqual(client.load_token(token_file), "receiver-identity")
        self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)

    def test_installs_desktop_login_hook_for_graphical_environment(self):
        home = pathlib.Path(self.state_directory.name) / "ubuntu home"
        client.install_desktop_session_hook(home)

        helper = home / ".local/bin/novnc-file-transfer-session-start"
        autostart = home / ".config/autostart/novnc-file-transfer-session.desktop"
        self.assertIn("import-environment", helper.read_text(encoding="utf-8"))
        self.assertIn("restart novnc-file-transfer.service", helper.read_text(encoding="utf-8"))
        self.assertEqual(helper.stat().st_mode & 0o777, 0o700)
        self.assertIn(f'Exec="{helper}"', autostart.read_text(encoding="utf-8"))

    def test_linux_clipboard_write_does_not_wait_on_xclip_background_process(self):
        with (mock.patch.dict("os.environ", {"DISPLAY": ":1"}),
              mock.patch.object(client.subprocess, "run") as run):
            client.write_desktop_clipboard("clipboard text")

        run.assert_called_once_with(
            ["xclip", "-selection", "clipboard", "-i"],
            input=b"clipboard text", check=True,
            stdout=client.subprocess.DEVNULL,
            stderr=client.subprocess.DEVNULL, timeout=3,
        )

    def test_clipboard_sync_requires_authorization_and_source_role(self):
        payload = json.dumps({"source": "browser", "text": "from Windows"}).encode()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/clipboard", "POST", payload, {"Content-Type": "application/json"})
        error.exception.close()
        self.assertEqual(error.exception.code, 401)

        with self.request(
            "/api/clipboard", "POST", payload,
            {"Authorization": "Bearer test-token", "Content-Type": "application/json"},
        ) as response:
            state = json.load(response)
        self.assertEqual(state, {"revision": 1, "source": "browser", "text": "from Windows"})
        with self.request(
            "/api/clipboard?since=1",
            headers={"Authorization": "Bearer test-token"},
        ) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(response.read(), b"")

        self.server.receiver_tokens["receiver-token"] = None
        with self.request(
            "/api/clipboard",
            headers={"Authorization": "Bearer receiver-token"},
        ) as response:
            self.assertEqual(json.load(response), state)

        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(
                "/api/clipboard", "POST", payload,
                {"Authorization": "Bearer receiver-token", "Content-Type": "application/json"},
            )
        error.exception.close()
        self.assertEqual(error.exception.code, 400)

    def test_clipboard_sync_session_is_admin_controlled_and_expires(self):
        with self.request(
            "/api/clipboard/active",
            headers={"Authorization": "Bearer test-token"},
        ) as response:
            self.assertFalse(json.load(response)["active"])

        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(
                "/api/clipboard/active", "POST", b'{"active":true}',
                {"Authorization": "Bearer receiver-token", "Content-Type": "application/json"},
            )
        error.exception.close()
        self.assertEqual(error.exception.code, 401)

        with self.request(
            "/api/clipboard/active", "POST", b'{"active":true}',
            {"Authorization": "Bearer test-token", "Content-Type": "application/json"},
        ):
            pass
        with self.request(
            "/api/clipboard/active",
            headers={"Authorization": "Bearer test-token"},
        ) as response:
            self.assertTrue(json.load(response)["active"])

        self.server.clipboard_active_until = time.time() - 1
        with self.request(
            "/api/clipboard/active",
            headers={"Authorization": "Bearer test-token"},
        ) as response:
            self.assertFalse(json.load(response)["active"])

    def test_linux_client_syncs_clipboard_both_directions(self):
        self.server.receiver_tokens["receiver-token"] = None
        receiver_headers = {"Authorization": "Bearer receiver-token"}
        with self.request(
            "/api/clipboard/active", "POST", b'{"active":true}',
            {"Authorization": "Bearer test-token", "Content-Type": "application/json"},
        ):
            pass
        with mock.patch.object(client, "read_desktop_clipboard", return_value="from Ubuntu"):
            local_text, revision = client.sync_clipboard_once(
                self.base_url, "receiver-token", None, 0,
            )
        self.assertEqual((local_text, revision), ("from Ubuntu", 1))
        with self.request("/api/clipboard", headers=receiver_headers) as response:
            self.assertEqual(json.load(response)["source"], "linux")
        with mock.patch.object(client, "read_desktop_clipboard", return_value="from Ubuntu"):
            unchanged_text, unchanged_revision = client.sync_clipboard_once(
                self.base_url, "receiver-token", "from Ubuntu", revision,
            )
        self.assertEqual((unchanged_text, unchanged_revision), ("from Ubuntu", revision))

        payload = json.dumps({"source": "browser", "text": "from Windows"}).encode()
        with self.request(
            "/api/clipboard", "POST", payload,
            {"Authorization": "Bearer test-token", "Content-Type": "application/json"},
        ):
            pass
        with (mock.patch.object(client, "read_desktop_clipboard", return_value="from Ubuntu"),
              mock.patch.object(client, "write_desktop_clipboard") as write_clipboard):
            local_text, revision = client.sync_clipboard_once(
                self.base_url, "receiver-token", "from Ubuntu", 1,
            )
        write_clipboard.assert_called_once_with("from Windows")
        self.assertEqual((local_text, revision), ("from Windows", 2))

    def test_linux_client_does_not_read_clipboard_without_active_browser_session(self):
        self.server.receiver_tokens["receiver-token"] = None
        with mock.patch.object(client, "read_desktop_clipboard") as read_clipboard:
            self.assertEqual(
                client.sync_clipboard_once(self.base_url, "receiver-token", "unchanged", 4),
                ("unchanged", 4),
            )
        read_clipboard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
