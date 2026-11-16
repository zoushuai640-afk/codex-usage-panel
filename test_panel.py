"""Run with Python; uses disposable fixtures only, never writes real Codex data."""
from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from server import ThreadingHTTPServer as HTTPServer

from server import APP_ID, Rollout, Store, canonical, handler
from quota import QuotaReader, make_request, normalize
from pricing import cost_usd


def event(kind, at=100, **fields):
    return {"type": "event_msg", "timestamp": at, "payload": {"type": kind, **fields}}


def token(total, output, context=1000):
    return event("token_count", info={"total_token_usage": {
        "input_tokens": total-output, "cached_input_tokens": 200,
        "output_tokens": output, "reasoning_output_tokens": 10, "total_tokens": total},
        "last_token_usage": {"total_tokens": 300}, "model_context_window": context})


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.log = self.home / "one.jsonl"
        self.log.write_text("", encoding="utf8")

    def tearDown(self):
        self.temp.cleanup()

    def append(self, *events):
        with self.log.open("a", encoding="utf8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

    def test_turns_duplicate_snapshots_and_exact_timing(self):
        self.append(event("task_started", turn_id="a"), token(1000, 100), token(1000, 100),
                    event("task_complete", at=110, turn_id="a", duration_ms=10000, time_to_first_token_ms=500),
                    event("task_started", at=120, turn_id="b"), token(1500, 140),
                    event("task_complete", at=124, turn_id="b", duration_ms=4000, time_to_first_token_ms=200))
        before = hashlib.sha256(self.log.read_bytes()).hexdigest()
        r = Rollout(self.log)
        s = r.snapshot(130)
        self.assertEqual(s["usage"]["total_tokens"], 1500)
        self.assertEqual(s["context_ratio"], .3)
        self.assertEqual(s["history"][0]["output_tokens"], 40)
        self.assertEqual(s["history"][0]["rate"], 10)
        self.assertEqual(s["history"][1]["rate"], 10)
        self.assertEqual(s["history"][0]["ttft_ms"], 200)
        self.assertEqual(len(r.snapshot(140)["history"]), 2)
        self.assertEqual(hashlib.sha256(self.log.read_bytes()).hexdigest(), before)

    def test_model_switch_duplicate_counts_and_previous_turn_usage(self):
        self.append({"type": "turn_context", "payload": {"model": "gpt-6.1-terra"}},
                    event("task_started", turn_id="a"), token(1000, 100), token(1000, 100),
                    event("task_complete", at=110, turn_id="a", duration_ms=10000),
                    {"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}},
                    event("task_started", at=120, turn_id="b"), token(1500, 140),
                    event("task_complete", at=124, turn_id="b", duration_ms=4000))
        s = Rollout(self.log).snapshot(125)
        models = {r["model"]: r for r in s["models"]}
        self.assertEqual(models["gpt-6.1-terra"]["total_tokens"], 1000)
        self.assertEqual(models["gpt-6.1-sol"]["total_tokens"], 500)
        self.assertAlmostEqual(models["gpt-6.1-terra"]["cost_usd"], .00121)
        last = s["history"][0]
        self.assertEqual(last["usage"]["input_tokens"], 460)
        self.assertEqual(last["usage"]["cached_input_tokens"], 0)
        self.assertEqual(last["usage"]["output_tokens"], 40)
        self.assertEqual(last["usage"]["total_tokens"], 500)
        self.assertAlmostEqual(last["cost_usd"], .00132)
        self.assertAlmostEqual(cost_usd("gpt-6.1-terra", {"input_tokens": 300000, "cached_input_tokens": 100000, "output_tokens": 1000}), .4175)
        self.assertIsNone(cost_usd("unknown-model", {}))

    def test_partial_line_and_incremental_refresh(self):
        self.append(event("task_started", turn_id="a"))
        raw = json.dumps(token(1000, 100)).encode()
        with self.log.open("ab") as f:
            f.write(raw[:30])
        r = Rollout(self.log)
        self.assertIsNone(r.snapshot(101)["usage"])
        with self.log.open("ab") as f:
            f.write(raw[30:] + b"\n")
        self.assertEqual(r.snapshot(110)["turn"]["rate"], 10)
        self.assertIsNone(r.snapshot(110)["turn"]["ttft_ms"])

    def test_reasoning_effort_tracks_logged_settings(self):
        self.append({"type": "turn_context", "payload": {"model": "gpt-6.1-sol", "effort": "high"}})
        r = Rollout(self.log)
        self.assertEqual(r.snapshot(100)["reasoning_effort"], "high")
        self.append(event("thread_settings_applied", thread_settings={"model": "gpt-6.1-sol", "reasoning_effort": "xhigh"}))
        self.assertEqual(r.snapshot(101)["reasoning_effort"], "xhigh")
        self.append({"type": "turn_context", "payload": {"model": "unknown"}})
        self.assertIsNone(r.snapshot(102)["reasoning_effort"])

    def test_recent_speed_uses_recent_samples_and_expires(self):
        first, second = token(1000, 100), token(1300, 160)
        first["timestamp"], second["timestamp"] = 101, 111
        self.append(event("task_started", turn_id="a"), first, second)
        r = Rollout(self.log)
        self.assertEqual(r.snapshot(112)["turn"]["recent_rate"], 6)
        self.assertIsNone(r.snapshot(142)["turn"]["recent_rate"])
        self.append(event("task_complete", at=143, turn_id="a", duration_ms=43000))
        self.assertIsNone(r.snapshot(144)["turn"]["recent_rate"])

    def test_reset_compaction_missing_timing_and_stale_state(self):
        self.append(token(1000, 100), event("task_started", turn_id="b"), token(500, 20), event("context_compacted"))
        r = Rollout(self.log)
        s = r.snapshot(400)
        self.assertIsNone(s["context_ratio"])
        self.assertIsNone(s["turn"]["rate"])
        self.assertEqual(s["turn"]["status"], "unconfirmed")
        self.append(event("task_complete", at=410, turn_id="b"))
        self.assertIsNone(r.snapshot(411)["turn"]["duration_ms"])

    def test_truncation_and_cross_thread_isolation(self):
        self.append(token(1000, 100))
        r = Rollout(self.log)
        r.snapshot(100)
        self.log.write_text("", encoding="utf8")
        self.assertIsNone(r.snapshot(110)["usage"])

        self.assertIsNone(r.snapshot(110)["context_used"])
        other = self.home / "other.jsonl"
        other.write_text(json.dumps(token(500, 50)) + "\n")
        self.assertEqual(Rollout(other).snapshot(100)["usage"]["total_tokens"], 500)
        self.assertIsNone(r.snapshot(110)["usage"])

    def test_windows_extended_path_uses_same_boundary(self):
        self.assertEqual(canonical("\\\\?\\" + str(self.log.resolve())), self.log.resolve())
        self.assertTrue(canonical("\\\\?\\" + str(self.log.resolve())).is_relative_to(self.home.resolve()))

    def test_database_readonly_and_http_boundaries(self):
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads(id,name,title,model,created_at,updated_at,rollout_path,archived,source)")
            db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?)", ("a", "真实标题", "raw prompt", "test-model", 90, 100, str(self.log), 0, "vscode"))
        self.append(token(1000, 100))
        store = Store(self.home)
        with closing(store.connect()) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM threads")
        self.assertEqual(store.snapshot("a")["selected"]["name"], "真实标题")
        self.assertEqual(store.snapshot("a")["quota"], {"status": "disabled", "rows": []})
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db, db:
            db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?)", ("b", "无记录", "", "model", 90, 99, str(self.home / "missing.jsonl"), 0, "vscode"))
        self.assertEqual([row["id"] for row in store.snapshot(None)["threads"]], ["a"])
        self.assertIsNone(store.snapshot("missing")["selected"])
        http = HTTPServer(("127.0.0.1", 0), handler(store))
        worker = threading.Thread(target=http.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{http.server_port}"
        try:
            # An idle browser preconnection must not block another request.
            with socket.create_connection(("127.0.0.1", http.server_port)):
                with urlopen(base + "/health", timeout=2) as response:
                    self.assertEqual(json.load(response)["app"], APP_ID)
            with urlopen(base + "/api?thread=a") as response:
                body = response.read().decode()
                self.assertEqual(json.loads(body)["app"], APP_ID)
                self.assertNotIn("raw prompt", body)
                self.assertNotIn(str(self.log), body)
            for req in [Request(base + "/api", headers={"Host": "evil.test"}),
                        Request(base + "/api", headers={"Origin": "https://evil.test"}),
                        Request(base + "/api", data=b"write"), Request(base + "/server.py")]:
                with self.assertRaises(HTTPError):
                    urlopen(req)
            payload = json.dumps({"action": "open", "thread": "00000000-0000-4000-8000-000000000001"}).encode()
            headers = {"Origin": base, "Content-Type": "application/json"}
            with patch("server.control_floating") as control:
                with urlopen(Request(base + "/floating", data=payload, headers=headers)) as response:
                    self.assertTrue(json.load(response)["ok"])
                control.assert_called_once_with("open", "00000000-0000-4000-8000-000000000001")
                for origin in ("https://evil.test", None):
                    bad_headers = {"Content-Type": "application/json"}
                    if origin:
                        bad_headers["Origin"] = origin
                    with self.assertRaises(HTTPError):
                        urlopen(Request(base + "/floating", data=payload, headers=bad_headers))
                with self.assertRaises(HTTPError):
                    urlopen(Request(base + "/floating", data=b'{"action":"open","thread":"../x"}', headers=headers))
                self.assertEqual(control.call_count, 1)
        finally:
            http.shutdown()
            http.server_close()
            worker.join()

    def test_official_quota_not_derived_from_token_shares(self):
        rows = [{"id": "a", "name": "A", "title": "prompt", "created_at": 90},
                {"id": "b", "name": "B", "title": "prompt", "created_at": 95}]
        body = make_request(rows, [("a", "child"), ("child", "grandchild")])
        self.assertEqual(body["threads"][0]["descendant_thread_ids"], ["child", "grandchild"])
        self.assertNotIn("prompt", json.dumps(body))
        response = {"data_as_of": "2026-10-12T09:27:21Z", "threads": [
            {"thread_id": "a", "data_status": "available", "weekly_limit_percent": 1.215409,
             "balance_usage_credits": "0E-10"},
            {"thread_id": "b", "data_status": "unavailable", "weekly_limit_percent": None}]}
        result = normalize(response, rows, body)
        self.assertEqual(result["rows"][0]["percent"], 1.215409)
        self.assertEqual(result["rows"][0]["credits"], 0)
        self.assertIsNone(result["rows"][1]["percent"])
        self.assertEqual(result["data_as_of"], response["data_as_of"])
        response["threads"][1].update(data_status="partial", weekly_limit_percent=.238737)
        partial = normalize(response, rows, body)["rows"][1]
        self.assertEqual(partial["percent"], .238737)
        self.assertTrue(partial["partial"])
        with self.assertRaises(ValueError):
            make_request(rows, [("a", "b")])

    def test_incomplete_quota_response_can_recover(self):
        (self.home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "test", "account_id": "test"}}))
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE thread_spawn_edges(parent_thread_id, child_thread_id)")
        store = Store(self.home)
        store.threads = lambda: []
        reader = QuotaReader(store)
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = http.client.IncompleteRead(b"partial", 10)
            reader.update()
            self.assertEqual(reader.value["status"], "error")
            response = opener.return_value.open.return_value.__enter__.return_value
            opener.return_value.open.side_effect = None
            response.read.return_value = b'{"threads": []}'
            reader.update()
            self.assertEqual(reader.value["status"], "ready")


if __name__ == "__main__":
    unittest.main(verbosity=2)
