"""
WebSocket hub -- pushes every bus event to connected dashboards.

Clients may subscribe to a subset of topics so a phone companion doesn't have
to receive 3-second metric samples. Slow clients are dropped rather than
allowed to back-pressure the whole application.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket

from .events import Event, bus
from .util import new_id, now_iso

logger = logging.getLogger("jarvis.ws")

# Topics a client receives unless it narrows the subscription.
DEFAULT_TOPICS = ["*"]
_SEND_TIMEOUT = 5.0
_QUEUE_LIMIT = 256


class Connection:
    def __init__(self, ws: WebSocket, user_id: Optional[int]):
        self.id = new_id("ws")
        self.ws = ws
        self.user_id = user_id
        self.topics: List[str] = list(DEFAULT_TOPICS)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        self.connected_at = now_iso()
        self.alive = True

    def wants(self, event_type: str) -> bool:
        for topic in self.topics:
            if topic == "*" or topic == event_type:
                return True
            if topic.endswith(".*") and event_type.startswith(topic[:-1]):
                return True
        return False


class WebSocketHub:
    def __init__(self) -> None:
        self._connections: Set[Connection] = set()
        self._unsubscribe = None

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._unsubscribe is None:
            self._unsubscribe = bus.subscribe("*", self._on_event)

    def stop(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None

    @property
    def client_count(self) -> int:
        return len(self._connections)

    def stats(self) -> Dict[str, Any]:
        return {
            "clients": len(self._connections),
            "connections": [
                {"id": c.id, "since": c.connected_at, "topics": c.topics, "backlog": c.queue.qsize()}
                for c in self._connections
            ],
        }

    # ------------------------------------------------------------------
    async def _on_event(self, event: Event) -> None:
        if not self._connections:
            return
        payload = event.to_dict()
        for conn in list(self._connections):
            if not conn.alive or not conn.wants(event.type):
                continue
            try:
                conn.queue.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning("ws client %s too slow, disconnecting", conn.id)
                conn.alive = False

    # ------------------------------------------------------------------
    async def serve(self, ws: WebSocket, user_id: Optional[int] = None) -> None:
        """Own the socket for its whole lifetime: a writer task drains the
        queue while this coroutine reads client control messages."""
        await ws.accept()
        conn = Connection(ws, user_id)
        self._connections.add(conn)
        logger.info("ws client connected (%s total)", len(self._connections))

        writer = asyncio.create_task(self._writer(conn))
        try:
            await ws.send_json(
                {
                    "type": "ws.hello",
                    "ts": now_iso(),
                    "data": {"connection_id": conn.id, "topics": conn.topics},
                }
            )
            while conn.alive:
                message = await ws.receive_json()
                await self._handle_client_message(conn, message)
        except Exception as exc:  # includes normal disconnects
            logger.debug("ws client %s closed: %s", conn.id, exc)
        finally:
            conn.alive = False
            writer.cancel()
            self._connections.discard(conn)
            logger.info("ws client disconnected (%s remain)", len(self._connections))

    async def _writer(self, conn: Connection) -> None:
        try:
            while conn.alive:
                payload = await conn.queue.get()
                try:
                    await asyncio.wait_for(conn.ws.send_json(payload), timeout=_SEND_TIMEOUT)
                except Exception:
                    conn.alive = False
                    return
        except asyncio.CancelledError:
            pass

    async def _handle_client_message(self, conn: Connection, message: Dict[str, Any]) -> None:
        kind = (message or {}).get("type")
        if kind == "subscribe":
            topics = message.get("topics")
            if isinstance(topics, list) and topics:
                conn.topics = [str(t) for t in topics]
            await conn.ws.send_json({"type": "ws.subscribed", "ts": now_iso(), "data": {"topics": conn.topics}})
        elif kind == "ping":
            await conn.ws.send_json({"type": "ws.pong", "ts": now_iso(), "data": {}})

    # ------------------------------------------------------------------
    async def close_all(self) -> None:
        for conn in list(self._connections):
            conn.alive = False
            try:
                await conn.ws.close()
            except Exception:
                pass
        self._connections.clear()


hub = WebSocketHub()
