"""Dedicated loopback webhook with authenticated, durable and deduplicated intake."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from gateway.app.max_api import MaxAPI
from gateway.app.max_bot import MaxBot

MAX_BODY_BYTES = 256 * 1024


class EventInbox:
    def __init__(self, database: Path):
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database, timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS events ("
                         "id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, "
                         "body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending')")
            # A crash must not replay a possibly completed create/delete operation.
            conn.execute("UPDATE events SET status='interrupted' WHERE status='processing'")

    def put(self, update: dict) -> None:
        kind = update.get("update_type")
        if kind == "message_created":
            identity = (update.get("message", {}).get("body") or {}).get("mid")
        elif kind == "message_callback":
            identity = (update.get("callback") or {}).get("callback_id")
        else:
            identity = hashlib.sha256(json.dumps(update, sort_keys=True).encode()).hexdigest()
        if not isinstance(identity, str) or not identity:
            raise ValueError("event identity is missing")
        with sqlite3.connect(self.database, timeout=10) as conn:
            conn.execute("INSERT OR IGNORE INTO events(key, body) VALUES (?, ?)",
                         (f"{kind}:{identity}", json.dumps(update, ensure_ascii=False)))

    def take(self) -> tuple[int, dict] | None:
        with sqlite3.connect(self.database, timeout=10) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT id, body FROM events WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return None
            conn.execute("UPDATE events SET status='processing' WHERE id=?", (row[0],))
        return int(row[0]), json.loads(row[1])

    def done(self, event_id: int) -> None:
        with sqlite3.connect(self.database, timeout=10) as conn:
            # Keep the receipt, discard message bodies which can contain client names.
            conn.execute("UPDATE events SET status='done', body='{}' WHERE id=?", (event_id,))


def create_app(bot: MaxBot, inbox: EventInbox, secret: str, *, run_worker: bool = True) -> FastAPI:
    if len(secret) < 32:
        raise ValueError("MAX webhook secret must contain at least 32 characters")
    wake, stop = threading.Event(), threading.Event()

    def worker() -> None:
        while not stop.is_set():
            try:
                event = inbox.take()
                if event is None:
                    wake.wait(1)
                    wake.clear()
                    continue
                event_id, update = event
                try:
                    bot.process(update)
                except Exception as exc:
                    print(f"MAX event failed: {type(exc).__name__}", flush=True)
                finally:
                    inbox.done(event_id)
            except Exception as exc:
                print(f"MAX inbox failed: {type(exc).__name__}", flush=True)
                stop.wait(2)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        thread = None
        if run_worker:
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
        yield
        stop.set()
        wake.set()
        if thread:
            await asyncio.to_thread(thread.join, 55)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.post("/max/webhook")
    async def webhook(request: Request):
        supplied = request.headers.get("X-Max-Bot-Api-Secret", "")
        if not hmac.compare_digest(supplied.encode(), secret.encode()):
            raise HTTPException(403)
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > MAX_BODY_BYTES:
                raise HTTPException(413)
        try:
            update = json.loads(body)
            if not isinstance(update, dict):
                raise ValueError()
            authorized = bot.authorized(update)
            if authorized:
                await asyncio.to_thread(inbox.put, update)
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(400) from None
        if authorized:
            wake.set()
        return {"success": True}

    return app


def production_app() -> FastAPI:
    state_dir = Path(os.getenv("AWG_GATEWAY_STATE_DIR", "/var/lib/awg-gateway"))
    bot = MaxBot(MaxAPI(os.environ["MAX_BOT_TOKEN"], os.getenv("MAX_CA_FILE") or None),
                 state_dir, os.environ["MAX_USER_ID"], os.environ["MAX_CHAT_ID"])
    return create_app(bot, EventInbox(state_dir / "max-events.sqlite3"), os.environ["MAX_WEBHOOK_SECRET"])
