"""Alpaca market-data source and the stdlib websocket client. Fixtures are written from the documented format
(docs.alpaca.markets real-time-stock-pricing-data and streaming-market-data, fetched 2026-10-23); the end-to-end test
runs against a local fake server on 127.0.0.1. Keys are TEST FIXTURES; no network access."""
from __future__ import annotations

import base64
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from unknown.broker.alpaca import stream as A
from unknown.broker.alpaca import ws
from unknown.marketdata import config as C
from unknown.marketdata.bus import MarketDataBus

# Documented examples (docs.alpaca.markets/docs/real-time-stock-pricing-data), symbol changed to AAPL for the quote.
DOC_QUOTE = '{"T":"q","S":"AAPL","bx":"U","bp":87.66,"bs":1,"ax":"Q","ap":87.68,"as":4,"t":"2021-02-22T15:51:45.335689322Z","c":["R"],"z":"C"}'
DOC_TRADE = '{"T":"t","i":96921,"S":"AAPL","x":"D","p":126.55,"s":1,"t":"2021-02-22T15:51:44.208Z","c":["@","I"],"z":"C"}'
FIXTURE_KEYS = {"key_id": "FIXTURE-KEY-ID", "secret_key": "FIXTURE-SECRET"}


class Recorder:
    def __init__(self):
        self.events = []

    def quote(self, sid, key, fields, **kw):
        self.events.append(("quote", key, fields, kw))

    def trade(self, sid, key, **kw):
        self.events.append(("trade", key, kw))

    def depth(self, *a, **kw):
        self.events.append(("depth", a, kw))

    def status(self, sid, key, state, reason):
        self.events.append(("status", key, state, reason))


class ParsingTests(unittest.TestCase):
    def test_rfc3339_nanoseconds(self):
        self.assertEqual(A.rfc3339_ns("2021-02-22T15:51:45.335689322Z"), 1614009105_335689322)
        self.assertEqual(A.rfc3339_ns("2021-02-22T15:51:44.208Z"), 1614009104_208000000)
        self.assertEqual(A.rfc3339_ns("2021-02-22T10:51:44-05:00"), 1614009104_000000000)
        self.assertIsNone(A.rfc3339_ns(None))

    def test_documented_messages_map_to_contract_fields_without_floats(self):
        src = A.AlpacaSource(credentials=FIXTURE_KEYS, venue_lookup=C.venue, run_thread=False, time_ns=lambda: 7)
        rec = Recorder()
        src.start(rec)
        self.assertTrue(src.subscribe("AAPL.XNAS").ok)
        src.handle(f"[{DOC_QUOTE},{DOC_TRADE}]")
        quote = rec.events[0]
        self.assertEqual(quote[2], {"bid": "87.66", "ask": "87.68", "bid_size": "1", "ask_size": "4"})
        self.assertEqual(quote[3]["ts_source_ns"], 1614009105_335689322)
        self.assertEqual((quote[3]["data_type"], quote[3]["size_unit"], quote[3]["feed"]),
                         ("REALTIME", "round_lots", "alpaca:iex"))
        trade = rec.events[1][2]
        self.assertEqual((trade["price"], trade["size"], trade["exchange"], trade["conditions"]),
                         ("126.55", "1", "D", ["@", "I"]))
        self.assertEqual(rec.events[2][2], {"last": "126.55", "last_size": "1"})

    def test_feeds_budgets_and_refusals(self):
        self.assertEqual(A.AlpacaSource(credentials=None, venue_lookup=C.venue).line_budget(), 30)
        sip = A.AlpacaSource(feed="sip", credentials=None, venue_lookup=C.venue)
        self.assertIsNone(sip.line_budget())
        self.assertEqual(A.AlpacaSource(feed="delayed_sip", credentials=None, venue_lookup=C.venue).data_type, "DELAYED")
        no_keys = A.AlpacaSource(credentials=None, venue_lookup=C.venue).subscribe("AAPL.XNAS")
        self.assertEqual((no_keys.ok, no_keys.state), (False, "UNAVAILABLE"))
        self.assertIn("secrets/alpaca.json", no_keys.reason)
        eu = A.AlpacaSource(credentials=FIXTURE_KEYS, venue_lookup=C.venue).subscribe("SAP.XETR")
        self.assertIn("US stocks", eu.reason)
        with self.assertRaises(ValueError):
            A.AlpacaSource(feed="opra", credentials=None, venue_lookup=C.venue)

    def test_credentials_only_from_the_secrets_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(A.load_credentials(Path(tmp)))
            (Path(tmp) / "secrets").mkdir()
            (Path(tmp) / "secrets" / "alpaca.json").write_text(json.dumps(FIXTURE_KEYS))
            self.assertEqual(A.load_credentials(Path(tmp)), FIXTURE_KEYS)
            src = A.build({"feed": "iex"}, Path(tmp))
            self.assertNotIn("FIXTURE-SECRET", json.dumps(src.describe()))


class WebSocketCodecTests(unittest.TestCase):
    def test_rfc6455_accept_key_example(self):
        self.assertEqual(ws.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_frames_round_trip_with_and_without_mask(self):
        for size in (0, 5, 125, 126, 65535, 65536, 70000):
            payload = os.urandom(size)
            for key in (None, b"\x01\x02\x03\x04"):
                data = ws.encode_frame(ws.OP_BINARY, payload, mask_key=key)
                buf = memoryview(data)
                pos = [0]

                def read_exact(n):
                    out = bytes(buf[pos[0]:pos[0] + n])
                    pos[0] += n
                    return out
                fin, op, got = ws.read_frame(read_exact)
                self.assertEqual((fin, op, got), (True, ws.OP_BINARY, payload))
                self.assertEqual(pos[0], len(data))
        self.assertEqual(ws.encode_frame(ws.OP_TEXT, b"Hello", mask_key=None), b"\x81\x05Hello")   # RFC 6455 5.7


class FakeAlpacaServer:
    """A local websocket server speaking the documented control flow (one connection)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received: list[dict] = []
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        conn, _ = self.sock.accept()
        rf = conn.makefile("rb")
        headers = {}
        rf.readline()
        while True:
            line = rf.readline().decode()
            if line in ("\r\n", ""):
                break
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {ws.accept_key(headers['sec-websocket-key'])}\r\n\r\n").encode())

        def send(obj):
            conn.sendall(ws.encode_frame(ws.OP_TEXT, json.dumps(obj).encode() if not isinstance(obj, str)
                                         else obj.encode(), mask_key=None))

        def recv():
            op = None
            while op != ws.OP_TEXT:              # skips the client's pong
                fin, op, payload = ws.read_frame(lambda n: rf.read(n))
            msg = json.loads(payload)
            self.received.append(msg)
            return msg
        send([{"T": "success", "msg": "connected"}])
        auth = recv()
        assert auth == {"action": "auth", "key": "FIXTURE-KEY-ID", "secret": "FIXTURE-SECRET"}, auth
        send([{"T": "success", "msg": "authenticated"}])
        sub = recv()
        send([{"T": "subscription", "trades": sub["trades"], "quotes": sub["quotes"], "bars": []}])
        conn.sendall(ws.encode_frame(ws.OP_PING, b"hb", mask_key=None))
        send(f"[{DOC_QUOTE},{DOC_TRADE}]")
        recv()                                    # the next subscribe request
        send([{"T": "error", "code": 405, "msg": "symbol limit exceeded"}])
        try:
            while True:
                fin, op, payload = ws.read_frame(lambda n: rf.read(n))
                if op == ws.OP_CLOSE:
                    break
        except Exception:
            pass
        conn.close()
        self.sock.close()


class EndToEndTests(unittest.TestCase):
    def test_connect_auth_subscribe_stream_and_symbol_limit(self):
        server = FakeAlpacaServer()
        src = A.AlpacaSource(credentials=FIXTURE_KEYS, venue_lookup=C.venue, url=f"ws://127.0.0.1:{server.port}/v2/iex",
                             run_thread=False)
        cfg = C.config_from_dict({"sources": {"ALPACA": {}}})
        bus = MarketDataBus(cfg, {"ALPACA": src})
        bus.start()
        client = bus.open_client(["AAPL.XNAS"])
        session = threading.Thread(target=lambda: self._run(src), daemon=True)
        session.start()
        items, deadline = [], time.monotonic() + 5
        while time.monotonic() < deadline and not any(n == "trade" for n, _ in items):
            items += client.drain(0.05)
        while time.monotonic() < deadline and not src._authenticated:
            time.sleep(0.01)
        trade = next(ev for n, ev in items if n == "trade")
        self.assertEqual((trade["source"], trade["price"], trade["ts_source_ns"]), ("ALPACA", "126.55",
                                                                                     "1614009104208000000"))
        self.assertIn("SUBSCRIBED", [ev["state"] for n, ev in items if n == "status"])
        second = bus.open_client(["MSFT.XNAS"])
        refused = []
        while time.monotonic() < deadline and not refused:
            refused = [ev for n, ev in second.drain(0.05) if n == "status" and ev["state"] == "LINE_BUDGET"]
        self.assertIn("405 symbol limit exceeded", refused[0]["reason"])
        self.assertEqual(server.received[1], {"action": "subscribe", "trades": ["AAPL"], "quotes": ["AAPL"]})
        self.assertEqual(server.received[2], {"action": "subscribe", "trades": ["MSFT"], "quotes": ["MSFT"]})
        src.stop()
        bus.stop()

    @staticmethod
    def _run(src):
        try:
            src.run_session()
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
