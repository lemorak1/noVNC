import importlib.util
import json
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer


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
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), relay.RelayHandler)
        self.server.token = "test-token"
        self.server.file_queue = relay.FileQueue()
        self.server.allowed_origin = "*"
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.server.file_queue.close()

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


if __name__ == "__main__":
    unittest.main()
