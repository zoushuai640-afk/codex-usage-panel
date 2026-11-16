"""Local, read-only Codex rollout telemetry. Python standard library only."""
import argparse
from collections import deque
from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit
from quota import QuotaReader
from pricing import cost_usd

APP_ID = "codex-token-panel-v1"
ROOT = Path(__file__).resolve().parent
TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
              "output_tokens", "reasoning_output_tokens", "total_tokens")


def canonical(path):
    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return Path(text).resolve()


def timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


class Rollout:
    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.model = None
        self.reasoning_effort = None
        self.usage = None
        self.last_usage = {}
        self.context = None
        self.sample_at = None
        self.event_at = None
        self.turn = None
        self.history = deque(maxlen=40)
        self.compacted = False
        self.error = None
        self.limits = None
        self.limits_at = None
        self.output_samples = deque(maxlen=2)
        self.by_model = {}

    def consume(self, record):
        kind, p = record.get("type"), record.get("payload", {})
        if not isinstance(p, dict):
            return
        when = timestamp(record.get("timestamp"))
        self.event_at = when or self.event_at
        if kind == "turn_context":
            self.model = p.get("model") or self.model
            self.reasoning_effort = p.get("effort") or p.get("reasoning_effort")
            if self.turn:
                self.turn["model"] = self.model
            return
        if kind != "event_msg":
            return
        event = p.get("type")
        if event == "token_count" and isinstance(p.get("rate_limits"), dict):
            limits = p["rate_limits"]
            if limits.get("limit_id") in (None, "codex"):
                self.limits, self.limits_at = limits, when
        if event == "thread_settings_applied":
            settings = p.get("thread_settings", {})
            self.model = settings.get("model") or self.model
            self.reasoning_effort = settings.get("reasoning_effort")
        elif event == "task_started":
            self.output_samples.clear()
            baseline = self.usage.get("output_tokens") if self.usage is not None else 0
            self.turn = {"id": p.get("turn_id"), "started_at": when or timestamp(p.get("started_at")),
                         "model": self.model, "baseline_output": baseline,
                         "status": "running", "duration_ms": None, "ttft_ms": None,
                         "output_tokens": None, "rate": None, "usage": None, "cost_usd": 0.0,
                         "usage_complete": True}
            self.context = p.get("model_context_window") or self.context
        elif event == "token_count" and isinstance(p.get("info"), dict):
            info = p["info"]
            total = info.get("total_token_usage")
            if not isinstance(total, dict):
                return
            previous = (self.usage or {}).get("output_tokens")
            current = total.get("output_tokens")
            delta = {k: total.get(k, 0) - (self.usage or {}).get(k, 0) for k in TOKEN_KEYS}
            if any(v < 0 for v in delta.values()):
                if self.turn:
                    self.turn["usage_complete"] = False
            elif delta["total_tokens"] > 0:
                model = self.model or "unknown"
                bucket = self.by_model.setdefault(model, {"model": model, **dict.fromkeys(TOKEN_KEYS, 0), "cost_usd": 0.0})
                cost = cost_usd(model, delta)
                for k in TOKEN_KEYS:
                    bucket[k] += delta[k]
                bucket["cost_usd"] = bucket["cost_usd"] + cost if bucket["cost_usd"] is not None and cost is not None else None
                if self.turn and self.turn.get("status") == "running":
                    usage = self.turn.get("usage") or dict.fromkeys(TOKEN_KEYS, 0)
                    for k in TOKEN_KEYS:
                        usage[k] += delta[k]
                    self.turn["usage"] = usage
                    old_cost = self.turn.get("cost_usd")
                    self.turn["cost_usd"] = old_cost + cost if old_cost is not None and cost is not None else None
            if self.turn and self.turn.get("status") == "running" and when is not None and current is not None:
                if self.output_samples and current < self.output_samples[-1][1]:
                    self.output_samples.clear()
                if not self.output_samples or when > self.output_samples[-1][0]:
                    self.output_samples.append((when, current))
            self.usage = dict(total)
            self.last_usage = info.get("last_token_usage") or {}
            self.context = info.get("model_context_window") or self.context
            self.sample_at = when
            self.compacted = False
            if self.turn and self.turn["status"] == "running":
                # A reset breaks the cumulative baseline; never invent a rate.
                if previous is not None and current is not None and current < previous:
                    self.turn["baseline_output"] = None
                baseline = self.turn["baseline_output"]
                if baseline is not None and current is not None:
                    self.turn["output_tokens"] = max(0, current - baseline)
        elif event in ("task_complete", "turn_aborted"):
            turn_id = p.get("turn_id")
            if not self.turn or (turn_id and turn_id != self.turn["id"]):
                self.turn = {"id": turn_id, "started_at": timestamp(p.get("started_at")),
                             "model": self.model, "output_tokens": None, "baseline_output": None}
            if self.turn.get("status") in ("complete", "aborted"):
                return
            self.turn.update(status="complete" if event == "task_complete" else "aborted",
                             completed_at=when, duration_ms=p.get("duration_ms"),
                             ttft_ms=p.get("time_to_first_token_ms"))
            duration, output = self.turn["duration_ms"], self.turn["output_tokens"]
            self.turn["rate"] = output / (duration / 1000) if duration and output is not None else None
            self.history.append(dict(self.turn))
        elif event == "context_compacted":
            self.compacted = True

    def refresh(self):
        try:
            stat = self.path.stat()
            if stat.st_size < self.offset:
                self.__init__(self.path)
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                for line in stream:
                    if not line.endswith(b"\n"):
                        break  # An in-flight JSON record is retried on the next poll.
                    self.offset += len(line)
                    if b'"event_msg"' not in line and b'"turn_context"' not in line:
                        continue
                    try:
                        self.consume(json.loads(line))
                    except (ValueError, TypeError):
                        continue
            self.error = None
        except OSError:
            self.error = "本地日志暂不可读；下次刷新重试"

    def snapshot(self, now):
        self.refresh()
        last = getattr(self, "last_usage", {})
        used = last.get("total_tokens")
        ratio = used / self.context if used is not None and self.context and not self.compacted else None
        turn = dict(self.turn) if self.turn else None
        if turn and turn.get("status") == "running":
            start = turn.get("started_at")
            turn["elapsed_ms"] = max(0, (now - start) * 1000) if start else None
            turn["rate"] = (turn["output_tokens"] / (turn["elapsed_ms"] / 1000)
                            if turn.get("output_tokens") is not None and turn.get("elapsed_ms") else None)
            # No recent log activity does not prove the task is still executing.
            turn["status"] = "running" if self.event_at and now - self.event_at < 180 else "unconfirmed"
        if turn:
            turn["recent_rate"] = None
            turn["recent_rate_at"] = None
            if turn.get("status") == "running" and len(self.output_samples) == 2:
                (first_time, first), (last_time, last) = self.output_samples
                if 0 < last_time - first_time <= 60 and 0 <= now - last_time <= 30:
                    turn["recent_rate"] = (last - first) / (last_time - first_time)
                    turn["recent_rate_at"] = last_time
        return {"usage": self.usage, "context_used": used, "context_window": self.context,
                "context_ratio": ratio, "compacted": self.compacted, "sample_at": self.sample_at,
                "event_at": self.event_at, "turn": turn, "history": list(reversed(self.history)),
                "model": self.model, "reasoning_effort": self.reasoning_effort,
                "models": list(self.by_model.values()), "error": self.error}


class Store:
    def __init__(self, home):
        self.home = canonical(home)
        self.logs = {}
        self.quota = QuotaReader(self)

    def connect(self):
        db = sqlite3.connect((self.home / "state_5.sqlite").as_uri() + "?mode=ro", uri=True, timeout=2)
        db.execute("PRAGMA query_only=ON")
        db.row_factory = sqlite3.Row
        return db

    def threads(self):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT id, name, title, model, created_at, updated_at, rollout_path FROM threads "
                              "WHERE archived=0 AND source NOT LIKE '%subagent%' ORDER BY updated_at DESC").fetchall()
        return [dict(row) for row in rows]

    def snapshot(self, selected):
        rows = []
        for row in self.threads():
            path = canonical(row["rollout_path"])
            if path.is_relative_to(self.home):
                log = self.logs.setdefault(str(path), Rollout(path))
                log.refresh()
                if not log.error and log.usage is not None:
                    rows.append(row)
        public = [{"id": r["id"], "name": r["name"] or r["title"] or "未命名对话",
                   "model": r["model"], "updated_at": r["updated_at"]} for r in rows]
        chosen = next((r for r in rows if r["id"] == selected), None) if selected else next(iter(rows), None)
        data = None
        if chosen:
            path = canonical(chosen["rollout_path"])
            if not path.is_relative_to(self.home):
                raise ValueError("日志路径不在本机 Codex 数据目录内")
            key = str(path)
            if key not in self.logs:
                self.logs[key] = Rollout(path)
            data = self.logs[key].snapshot(time.time())
            data.update(id=chosen["id"], name=chosen["name"] or chosen["title"] or "未命名对话")
            data["model"] = data["model"] or chosen["model"]
        weekly = None
        for row in rows:
            path = canonical(row["rollout_path"])
            if not path.is_relative_to(self.home):
                continue
            log = self.logs.setdefault(str(path), Rollout(path))
            log.refresh()
            for window in (log.limits or {}).get("primary"), (log.limits or {}).get("secondary"):
                if window and window.get("window_minutes") == 10080:
                    if weekly is None or (log.limits_at or 0) > (weekly["sample_at"] or 0):
                        weekly = {**window, "sample_at": log.limits_at}
        quota = {"status": "disabled", "rows": []}
        model_totals = {}
        for row in rows:
            for model, usage in self.logs[str(canonical(row["rollout_path"]))].by_model.items():
                bucket = model_totals.setdefault(model, {"model": model, **dict.fromkeys(TOKEN_KEYS, 0), "cost_usd": 0.0})
                for k in TOKEN_KEYS:
                    bucket[k] += usage[k]
                bucket["cost_usd"] = bucket["cost_usd"] + usage["cost_usd"] if bucket["cost_usd"] is not None and usage["cost_usd"] is not None else None
        return {"app": APP_ID, "now": time.time(), "threads": public, "selected": data,
                "weekly_account": weekly, "quota": quota, "models": list(model_totals.values()),
                "error": "该对话没有可用的本地记录（也可能已归档）" if selected and not chosen else None}


def handler(store):
    snapshot_lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            authority = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != authority or self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.send_error(403)
                return
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + authority:
                self.send_error(403)
                return
            url = urlsplit(self.path)
            try:
                if url.path == "/health":
                    body, mime = json.dumps({"app": APP_ID, "pid": os.getpid()}).encode(), "application/json"
                elif url.path == "/api":
                    with snapshot_lock:
                        data = store.snapshot(parse_qs(url.query).get("thread", [None])[0])
                    body, mime = json.dumps(data, ensure_ascii=False).encode(), "application/json"
                elif url.path == "/":
                    body, mime = (ROOT / "panel.html").read_bytes(), "text/html"
                else:
                    self.send_error(404)
                    return
            except (OSError, sqlite3.Error, ValueError):
                body, mime = json.dumps({"error": "本地数据源暂不可用，请稍后重试"}, ensure_ascii=False).encode(), "application/json"
                self.send_response(503)
            else:
                self.send_response(200)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)
    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    store = Store(args.home)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(store))
    print(f"{APP_ID} http://127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever()
