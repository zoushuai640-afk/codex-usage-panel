"""Read the same usage-metadata endpoint used by the installed Codex client."""
from contextlib import closing
from datetime import datetime, timezone
import http.client
import json
import math
import sqlite3
import threading
import time
import urllib.error
import urllib.request

ENDPOINT = "https://chatgpt.com/backend-api/wham/usage/thread_usage/query_v2"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward the account bearer token to a redirect target.


def finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def make_request(rows, edges):
    children = {}
    for parent, child in edges:
        children.setdefault(parent, []).append(child)
    requests = []
    claimed = set()
    for row in rows[:100]:
        seen, pending = set(), list(children.get(row["id"], []))
        while pending:
            child = pending.pop()
            if child in seen or child == row["id"]:
                continue
            seen.add(child)
            pending.extend(children.get(child, []))
        group = seen | {row["id"]}
        if claimed & group or len(claimed | group) > 1000:
            raise ValueError("Usage groups overlap or exceed the client batch limit")
        claimed.update(group)
        requests.append({"thread_id": row["id"],
                         "created_at": datetime.fromtimestamp(row["created_at"], timezone.utc).isoformat(),
                         "descendant_thread_ids": sorted(seen)})
    return {"threads": requests}


def normalize(response, rows, body):
    values = {r["thread_id"]: r for r in response.get("threads", [])}
    child_counts = {r["thread_id"]: len(r["descendant_thread_ids"]) for r in body["threads"]}
    result = []
    for row in rows:
        usage = values.get(row["id"], {})
        available = usage.get("data_status") in ("available", "partial")
        result.append({"id": row["id"], "name": row["name"] or row["title"] or "未命名对话",
                       "percent": finite(usage.get("weekly_limit_percent")) if available else None,
                       "credits": finite(usage.get("balance_usage_credits")) if available else None,
                       "subagents": child_counts.get(row["id"], 0),
                       "partial": usage.get("data_status") == "partial"})
    result.sort(key=lambda r: (r["percent"] is None, -(r["percent"] or 0)))
    return {"status": "ready", "fetched_at": time.time(), "data_as_of": response.get("data_as_of"),
            "rows": result, "error": None}


class QuotaReader:
    def __init__(self, store):
        self.store = store
        self.account = None
        self.value = {"status": "loading", "rows": [], "error": None}

    def update(self):
        try:
            auth = json.loads((self.store.home / "auth.json").read_text(encoding="utf-8"))
            tokens = auth.get("tokens") or {}
            account = tokens.get("account_id")
            if self.account != account:
                self.value = {"status": "loading", "rows": [], "error": None}
                self.account = account
            if not tokens.get("access_token") or not account:
                raise PermissionError("ChatGPT sign-in required")
            rows = self.store.threads()[:100]
            with closing(self.store.connect()) as db:
                edges = db.execute("SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges").fetchall()
            body = make_request(rows, edges)
            request = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(), method="POST", headers={
                "Authorization": "Bearer " + tokens["access_token"], "ChatGPT-Account-Id": account,
                "Content-Type": "application/json", "Accept": "application/json"})
            with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
                result = json.load(response)
            self.value = normalize(result, rows, body)
        except (OSError, ValueError, KeyError, sqlite3.Error, http.client.HTTPException) as error:
            # Keep last good numbers marked stale; never log tokens or raw responses.
            message = "官方统计暂不可用；保留上次结果，5 分钟后重试"
            if isinstance(error, urllib.error.HTTPError) and error.code in (401, 403):
                message = "官方统计暂不可用，请确认 Codex 登录仍有效"
            self.value = {**self.value, "status": "error", "error": message}

    def start(self):
        def loop():
            while True:
                self.update()
                time.sleep(300)
        threading.Thread(target=loop, name="official-usage-readonly", daemon=True).start()
