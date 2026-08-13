# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from vg.main import (
    can_bind_port,
    choose_listen_port,
    find_free_port,
    looks_like_gallery_status,
    port_accepting,
    probe_own_gallery,
)


def _gallery_payload() -> bytes:
    return json.dumps(
        {
            "app": "video-gallery",
            "scanning": False,
            "lan_share": False,
            "lib_gen": 0,
            "thumb_progress": "",
        }
    ).encode("utf-8")


class _JsonHandler(BaseHTTPRequestHandler):
    payload = b"{}"

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, fmt: str, *args) -> None:
        return


def _serve(payload: bytes) -> tuple[ThreadingHTTPServer, int, threading.Thread]:
    class Handler(_JsonHandler):
        pass

    Handler.payload = payload
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = int(httpd.server_address[1])
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    for _ in range(80):
        if port_accepting(port):
            break
        time.sleep(0.01)
    else:
        httpd.shutdown()
        httpd.server_close()
        raise RuntimeError(f"test HTTP server did not start on {port}")
    return httpd, port, thread


def _stop(httpd: ThreadingHTTPServer) -> None:
    httpd.shutdown()
    httpd.server_close()


class ListenPortTests(unittest.TestCase):
    def test_status_payload_with_app_id_is_ours(self) -> None:
        self.assertTrue(looks_like_gallery_status({"app": "video-gallery"}))
        self.assertFalse(looks_like_gallery_status({"app": "other"}))
        self.assertTrue(
            looks_like_gallery_status(
                {
                    "scanning": False,
                    "lan_share": False,
                    "lib_gen": 1,
                    "thumb_progress": "",
                }
            )
        )
        self.assertFalse(looks_like_gallery_status({"ok": True}))

    def test_probe_recognizes_running_gallery_and_rejects_other_http(self) -> None:
        ours, our_port, _ = _serve(_gallery_payload())
        other, other_port, _ = _serve(b'{"ok": true, "name": "something-else"}')
        try:
            self.assertTrue(probe_own_gallery(our_port))
            self.assertFalse(probe_own_gallery(other_port))
        finally:
            _stop(ours)
            _stop(other)

    def test_find_free_port_skips_occupied_port(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        occupied = int(sock.getsockname()[1])
        try:
            sock.listen(1)
            found = find_free_port("127.0.0.1", occupied, attempts=5)
            self.assertIsNotNone(found)
            self.assertNotEqual(found, occupied)
            self.assertTrue(can_bind_port("127.0.0.1", int(found)))
        finally:
            sock.close()

    def test_choose_reuses_existing_gallery(self) -> None:
        httpd, port, _ = _serve(_gallery_payload())
        try:
            chosen, mode = choose_listen_port("127.0.0.1", port, locked=False)
            self.assertEqual(mode, "reuse")
            self.assertEqual(chosen, port)
        finally:
            _stop(httpd)

    def test_choose_skips_foreign_http_when_unlocked(self) -> None:
        httpd, port, _ = _serve(b'{"hello": "world"}')
        try:
            chosen, mode = choose_listen_port("127.0.0.1", port, locked=False, attempts=8)
            self.assertEqual(mode, "bind")
            self.assertIsNotNone(chosen)
            self.assertNotEqual(chosen, port)
        finally:
            _stop(httpd)

    def test_choose_fails_closed_when_locked_to_foreign_port(self) -> None:
        httpd, port, _ = _serve(b'{"hello": "world"}')
        try:
            chosen, mode = choose_listen_port("127.0.0.1", port, locked=True)
            self.assertIsNone(chosen)
            self.assertEqual(mode, "occupied")
        finally:
            _stop(httpd)

    def test_choose_binds_preferred_when_free(self) -> None:
        port = find_free_port("127.0.0.1", 18000, attempts=50)
        self.assertIsNotNone(port)
        chosen, mode = choose_listen_port("127.0.0.1", int(port), locked=True)
        self.assertEqual(mode, "bind")
        self.assertEqual(chosen, port)

    def test_open_when_ready_does_not_open_foreign_page(self) -> None:
        from vg import main as vg_main

        httpd, port, _ = _serve(b'{"hello": "world"}')
        opened: list[str] = []
        try:
            with patch.object(vg_main.webbrowser, "open", side_effect=opened.append):
                vg_main._open_when_ready(f"http://127.0.0.1:{port}", port, timeout=0.4)
                vg_main.time.sleep(0.55)
            self.assertEqual(opened, [])
        finally:
            _stop(httpd)


if __name__ == "__main__":
    unittest.main()
