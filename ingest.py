"""HTTP-сервер приёма снапшотов таблицы от Apps Script (push-модель).

Apps Script не может быть вызван анонимно (домен MGCom), но сам может слать
исходящие запросы. Поэтому он раз в N минут POST-ит содержимое листа сюда,
а бот сохраняет его в data/snapshot.json и читает оттуда.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from config import Config

logger = logging.getLogger("mgcom_nb_bot.ingest")

MAX_BODY_BYTES = 25 * 1024 * 1024  # 25 МБ — с запасом на большую таблицу


def _atomic_write(path: str, raw: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, path)


def _make_handler(cfg: Config):
    class IngestHandler(BaseHTTPRequestHandler):
        server_version = "mgcom-nb-bot/1.0"

        def _reply(self, code: int, text: str) -> None:
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _token_ok(self) -> bool:
            header_token = self.headers.get("X-Auth-Token", "")
            query_token = parse_qs(urlparse(self.path).query).get("token", [""])[0]
            provided = header_token or query_token
            return bool(provided) and provided == cfg.ingest_token

        def do_GET(self) -> None:  # health-check
            if urlparse(self.path).path == "/health":
                self._reply(200, "ok")
            else:
                self._reply(404, "not found")

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/ingest":
                self._reply(404, "not found")
                return
            if not self._token_ok():
                self._reply(403, "forbidden")
                return

            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0 or length > MAX_BODY_BYTES:
                self._reply(413, "bad length")
                return

            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._reply(400, "invalid json")
                return

            if not isinstance(data, dict) or "header" not in data or "rows" not in data:
                self._reply(400, "expected {header, rows}")
                return

            _atomic_write(cfg.snapshot_path, raw)
            logger.info(
                "Снапшот получен: %d строк, updated_at=%s",
                len(data.get("rows", [])),
                data.get("updated_at"),
            )
            self._reply(200, "ok")

        def log_message(self, fmt: str, *args) -> None:  # тише в логах
            logger.debug("%s - %s", self.address_string(), fmt % args)

    return IngestHandler


def start_ingest_server(cfg: Config) -> ThreadingHTTPServer:
    """Поднимает HTTP-сервер приёма снапшотов в фоновом потоке."""
    server = ThreadingHTTPServer((cfg.ingest_host, cfg.ingest_port), _make_handler(cfg))
    thread = threading.Thread(target=server.serve_forever, name="ingest", daemon=True)
    thread.start()
    logger.info("Ingest-сервер слушает %s:%d (POST /ingest)", cfg.ingest_host, cfg.ingest_port)
    return server
