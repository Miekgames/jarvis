"""Conversation, voice and command-history routes."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..container import container
from ..core.util import truncate
from .deps import current_user

router = APIRouter()


# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    purpose: str = "conversation"
    source: str = "text"
    stream: bool = True


# How long after a detection a transcript may arrive and still be its
# command. Capture plus transcription is a few seconds; a minute later
# belongs to something else entirely.
COMMAND_PAIRING_WINDOW_S = 60


def _reject_wake_word_source(source: str) -> None:
    """Spoken commands are run by the backend, not submitted by the page.

    The wake word used to be answered by whichever page received the event,
    which meant *every* open page answered: one sentence produced five
    conversations and five concurrent calls to a local model that serves them
    one at a time, so most came back empty. The detector now runs the command
    itself, exactly once.

    This refuses the old path outright rather than trusting every client to
    have been updated -- a stale tab left open overnight would otherwise
    quietly bring the duplicates back.
    """
    if source == "wake_word":
        raise HTTPException(
            status_code=409,
            detail="Spoken commands are run by the wake-word service, not submitted by the "
                   "interface. This page is running an older version -- reload it.",
        )


@router.post("/ai/chat", tags=["ai"])
async def chat(payload: ChatRequest, user: Dict[str, Any] = Depends(current_user)):
    """Server-sent events. Each line is `data: {json}`.

    Event types: start, delta, tool_start, tool_end, final, error.
    """
    orchestrator = container.get("orchestrator")
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="Message is empty.")
    _reject_wake_word_source(payload.source)

    async def event_stream():
        try:
            async for event in orchestrator.chat(
                payload.message,
                conversation_id=payload.conversation_id,
                purpose=payload.purpose,
                source=payload.source,
                stream=payload.stream,
            ):
                yield f"data: {json.dumps(event, default=str)}\n\n"
        except Exception as exc:  # never leave the stream hanging
            yield f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.post("/ai/ask", tags=["ai"])
async def ask(payload: ChatRequest, user: Dict[str, Any] = Depends(current_user)):
    """Non-streaming variant, for scripts and the mobile companion."""
    orchestrator = container.get("orchestrator")
    _reject_wake_word_source(payload.source)
    final_text = ""
    tools_used: List[str] = []
    tools_attempted: List[str] = []
    conversation_id = payload.conversation_id

    async for event in orchestrator.chat(
        payload.message, conversation_id=conversation_id, purpose=payload.purpose,
        source=payload.source, stream=False,
    ):
        if event["type"] == "tool_end":
            # Recorded on completion, not on start: a local model that invents
            # a tool name produces a tool_start for a tool that does not exist,
            # and reporting it as "used" would be a claim we cannot support.
            if event.get("ok"):
                tools_used.append(event["tool"])
            else:
                tools_attempted.append(event["tool"])
        elif event["type"] == "final":
            final_text = event["text"]
            conversation_id = event["conversation_id"]
        elif event["type"] == "error":
            raise HTTPException(status_code=502, detail=event["error"])

    return {"text": final_text, "conversation_id": conversation_id,
            "tools_used": tools_used,
            "tools_failed": tools_attempted}


# ---------------------------------------------------------------------------
@router.get("/ai/conversations", tags=["ai"])
async def conversations(limit: int = Query(30, le=200), user: Dict[str, Any] = Depends(current_user)):
    rows = await container.get("db").fetch_all(
        "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count"
        " FROM conversations c WHERE archived = 0 ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    )
    return {"conversations": rows}


@router.get("/ai/conversations/{conversation_id}", tags=["ai"])
async def conversation(conversation_id: str, user: Dict[str, Any] = Depends(current_user)):
    orchestrator = container.get("orchestrator")
    messages = await orchestrator.transcript(conversation_id)
    if not messages:
        raise HTTPException(status_code=404, detail="No such conversation.")
    return {"conversation_id": conversation_id, "messages": messages}


@router.delete("/ai/conversations/{conversation_id}", tags=["ai"])
async def delete_conversation(conversation_id: str, user: Dict[str, Any] = Depends(current_user)):
    db = container.get("db")
    await db.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
    deleted = await db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    await container.get("audit").record(
        "data.deleted", subject="conversation", detail={"id": conversation_id}
    )
    return {"deleted": bool(deleted)}


# ---------------------------------------------------------------------------
@router.get("/ai/tools", tags=["ai"])
async def tools(user: Dict[str, Any] = Depends(current_user)):
    registry = container.get("registry")
    permissions = container.get("permissions")
    enabled = {schema["name"] for schema in registry.schemas()}
    return {"tools": [
        {**tool.describe(), "enabled": tool.name in enabled,
         "permission_granted": permissions.allowed(tool.permission) if tool.permission else True}
        for tool in registry.all()
    ]}


@router.get("/ai/usage", tags=["ai"])
async def usage(hours: int = Query(24, le=24 * 30), user: Dict[str, Any] = Depends(current_user)):
    return await container.get("orchestrator").usage_summary(hours)


@router.get("/ai/efficiency", tags=["ai"])
async def efficiency(hours: int = Query(24, ge=1, le=24 * 365),
                     user: Dict[str, Any] = Depends(current_user)):
    """Replies, tokens, GPU energy, speed, and the cloud cost avoided."""
    from ..systemmon.energy import gpu_name

    summary = await container.get("orchestrator").efficiency_summary(hours)
    summary["gpu"] = gpu_name()
    return summary


@router.get("/ai/providers", tags=["ai"])
async def providers(user: Dict[str, Any] = Depends(current_user)):
    orchestrator = container.get("orchestrator")
    status = await orchestrator.provider_status()
    ollama = orchestrator.providers.get("ollama")

    local_models: List[str] = []
    local_routing: Dict[str, Any] = {}
    local_capabilities: Dict[str, Any] = {}
    if ollama is not None and status.get("ollama", {}).get("available"):
        local_models = await ollama.models()
        local_routing = await ollama.routing_table()
        local_capabilities = await ollama.capabilities()

    purposes = ("conversation", "fast", "coding", "reasoning", "vision")
    return {
        "providers": status,
        "ollama_models": local_models,
        "ollama_capabilities": local_capabilities,
        "routing": {
            purpose: {
                "cloud": orchestrator.model_for(purpose),
                "local": local_routing.get(purpose),
            }
            for purpose in purposes
        },
        "vision": await container.get("vision").available() if container.get("vision") else {},
    }


@router.get("/ai/engines", tags=["ai"])
async def engines(user: Dict[str, Any] = Depends(current_user)):
    """Local model servers that are running, and how the models fit the GPU."""
    from ..ai.engines import overview

    settings = container.settings
    found = await overview(settings.OLLAMA_HOST, int(getattr(settings, "PORT", 8000) or 8000))
    store = container.get("settings_store")
    found["local_server"] = ((store.get_cached("ai", {}) if store else {}) or {}).get(
        "local_server") or {}
    return found


@router.post("/ai/engines/test", tags=["ai"])
async def test_engine(payload: Dict[str, Any], user: Dict[str, Any] = Depends(current_user)):
    """Send one short message to a local server before switching to it."""
    from ..ai.providers.base import ProviderError
    from ..ai.providers.openai_compat_provider import OpenAICompatProvider

    provider = OpenAICompatProvider(str(payload.get("base_url") or ""),
                                    model=str(payload.get("model") or ""))
    try:
        completion = await provider.complete(
            [{"role": "user", "content": "Reply with the single word: ready"}],
            max_tokens=16, temperature=0.0)
        return {"ok": True, "reply": completion.text[:80], "model": completion.model,
                "latency_ms": completion.latency_ms}
    except ProviderError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        await provider.close()


# ---------------------------------------------------------------------------
# command history
# ---------------------------------------------------------------------------
@router.get("/commands", tags=["ai"])
async def commands(
    limit: int = Query(50, le=500),
    favourites_only: bool = False,
    search: Optional[str] = None,
    user: Dict[str, Any] = Depends(current_user),
):
    sql = "SELECT * FROM commands WHERE 1=1"
    params: List[Any] = []
    if favourites_only:
        sql += " AND (favorite = 1 OR pinned = 1)"
    if search:
        sql += " AND text LIKE ?"
        params.append(f"%{search}%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return {"commands": await container.get("db").fetch_all(sql, params)}


class CommandUpdate(BaseModel):
    pinned: Optional[bool] = None
    favorite: Optional[bool] = None
    label: Optional[str] = None


@router.patch("/commands/{command_id}", tags=["ai"])
async def update_command(
    command_id: int, payload: CommandUpdate, user: Dict[str, Any] = Depends(current_user)
):
    updates = {k: v for k, v in payload.dict(exclude_none=True).items()}
    if not updates:
        return {"ok": True}
    for key in ("pinned", "favorite"):
        if key in updates:
            updates[key] = 1 if updates[key] else 0
    assignments = ", ".join(f"{key} = ?" for key in updates)
    await container.get("db").execute(
        f"UPDATE commands SET {assignments} WHERE id = ?", (*updates.values(), command_id)
    )
    return {"ok": True}


@router.delete("/commands/{command_id}", tags=["ai"])
async def delete_command(command_id: int, user: Dict[str, Any] = Depends(current_user)):
    deleted = await container.get("db").execute("DELETE FROM commands WHERE id = ?", (command_id,))
    return {"deleted": bool(deleted)}


# ---------------------------------------------------------------------------
# voice
# ---------------------------------------------------------------------------
class SpeakRequest(BaseModel):
    text: str
    voice: Optional[str] = None
    rate: float = 1.0


@router.post("/voice/speak", tags=["voice"])
async def speak(payload: SpeakRequest, user: Dict[str, Any] = Depends(current_user)):
    """Return MP3 audio for the given text."""
    voice = container.get("voice")
    try:
        audio = await voice.synthesise(payload.text, payload.voice, payload.rate)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Content-Disposition": 'inline; filename="speech.mp3"', "Cache-Control": "no-store"},
    )


@router.post("/voice/transcribe", tags=["voice"])
async def transcribe(request: Request, user: Dict[str, Any] = Depends(current_user)):
    """Raw audio bytes in the request body (webm/ogg/wav). Kept body-raw rather
    than multipart so no extra dependency is needed."""
    audio = await request.body()
    if not audio:
        raise HTTPException(status_code=400, detail="No audio received.")
    if len(audio) > 25_000_000:
        raise HTTPException(status_code=413, detail="Audio too large (limit 25 MB).")

    filename = request.headers.get("x-filename", "audio.webm")
    voice = container.get("voice")
    try:
        result = await voice.transcribe(audio, filename)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return result


@router.get("/voice/voices", tags=["voice"])
async def voices(language: str = "en", user: Dict[str, Any] = Depends(current_user)):
    return {"voices": await container.get("voice").voices(language)}


class VoiceStateRequest(BaseModel):
    state: str
    text: str = ""
    interrupted: bool = False


@router.post("/voice/state", tags=["voice"])
async def voice_state(payload: VoiceStateRequest, user: Dict[str, Any] = Depends(current_user)):
    """The browser tells the backend when it starts/stops speaking or listening,
    so the particle field and the AI's state stay in sync with reality."""
    voice = container.get("voice")
    orchestrator = container.get("orchestrator")

    # While the page holds the microphone for a command, the always-on
    # detector stops scoring: two listeners on one device would otherwise
    # handle the same utterance twice, and the wake word would fire on the
    # command the user is in the middle of giving.
    hotword = container.get("hotword")

    if payload.state == "speaking_start":
        voice.speaking_started(payload.text)
        orchestrator.set_state("speaking")
    elif payload.state == "speaking_end":
        voice.speaking_ended(payload.interrupted)
        orchestrator.set_state("idle")
    elif payload.state == "listening_start":
        voice.listening_started()
        orchestrator.set_state("listening")
        if hotword is not None:
            hotword.suspend(30.0)
    elif payload.state == "listening_end":
        voice.listening_ended(payload.text)
        orchestrator.set_state("idle")
        if hotword is not None:
            hotword.resume()
    else:
        raise HTTPException(status_code=400, detail=f"Unknown voice state '{payload.state}'.")
    return {"ok": True, "state": orchestrator.state}


# ---------------------------------------------------------------------------
# wake word
#
# Continuous microphone capture, so every one of these is explicit: nothing
# here starts listening as a side effect of being asked about.
# ---------------------------------------------------------------------------
@router.get("/voice/hotword", tags=["voice"])
async def hotword_status(user: Dict[str, Any] = Depends(current_user)):
    """What the wake-word detector is doing, and on which device."""
    hotword = container.get("hotword")
    if hotword is None:
        return {"running": False, "phase": "failed", "available": False,
                "detail": "The wake-word detector is not available in this build."}
    return hotword.status()


class HotwordStartRequest(BaseModel):
    device: Optional[int] = None
    model: Optional[str] = None
    threshold: Optional[float] = None


@router.post("/voice/hotword/start", tags=["voice"])
async def hotword_start(payload: HotwordStartRequest,
                        user: Dict[str, Any] = Depends(current_user)):
    hotword = container.get("hotword")
    if hotword is None:
        raise HTTPException(status_code=503,
                            detail="The wake-word detector is not available in this build.")
    status = await hotword.start(device=payload.device, model=payload.model,
                                 threshold=payload.threshold)
    if status["phase"] == "failed":
        # A 200 saying "failed" invites the UI to show a green light over a
        # detector that is not running. Make it an error.
        raise HTTPException(status_code=503, detail=status["detail"])
    return status


@router.post("/voice/hotword/stop", tags=["voice"])
async def hotword_stop(user: Dict[str, Any] = Depends(current_user)):
    hotword = container.get("hotword")
    if hotword is None:
        raise HTTPException(status_code=503,
                            detail="The wake-word detector is not available in this build.")
    return await hotword.stop()


@router.get("/voice/devices", tags=["voice"])
async def voice_devices(user: Dict[str, Any] = Depends(current_user)):
    """Every microphone, opened and measured.

    Listing device names is not enough: on the machine this was written for,
    25 of 26 inputs -- the OS default among them -- open successfully and
    return pure silence. `signal` is the field that matters.
    """
    from ..voice.hotword import HotwordUnavailable, choose_device, probe_devices

    try:
        devices = await asyncio.get_running_loop().run_in_executor(None, probe_devices)
    except HotwordUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not inspect audio devices: {exc}")

    recommended = choose_device(devices)
    live = [d for d in devices if d["signal"]]
    if live:
        note = "{} of {} inputs are carrying audio.".format(len(live), len(devices))
    else:
        note = ("No input is carrying audio. Every device is muted, unplugged, or virtual "
                "with nothing behind it -- check Windows sound settings before blaming "
                "the wake word.")
    return {"devices": devices, "recommended": recommended, "note": note}


@router.post("/voice/listen", tags=["voice"])
async def voice_listen(user: Dict[str, Any] = Depends(current_user)):
    """Record a command on the backend's microphone, as the wake word does.

    What the microphone button calls when local capture is available. The
    browser records through whatever input Chrome prefers, which is not
    necessarily the one that works; this uses the device the detector has
    already proven.
    """
    hotword = container.get("hotword")
    if hotword is None:
        raise HTTPException(status_code=503,
                            detail="Local voice capture is not available in this build.")
    result = await hotword.listen_now()
    if not result["ok"]:
        # 409, not 500: the caller is expected to fall back to the browser.
        raise HTTPException(status_code=409, detail=result["reason"])
    return result


# ---------------------------------------------------------------------------
def pair_voice_events(rows: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """Match each wake-word detection with the command that followed it.

    The rows arrive newest first, which is the whole subtlety: a transcript is
    logged *after* the detection it belongs to, so in this order the detection
    appears **later** in the list, not earlier. Pairing forwards gets it
    exactly wrong -- it hands a transcript to the next detection up, which is
    a different, newer utterance.

    So a transcript is held until its detection turns up. One left over at the
    end had no detection before it at all, which is what push-to-talk looks
    like.
    """
    from datetime import datetime

    def parsed(value: str):
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    events: List[Dict[str, Any]] = []
    pending: Optional[Dict[str, Any]] = None

    def flush_pending() -> None:
        """A transcript with no detection under it: spoken without the word."""
        nonlocal pending
        if pending is not None:
            events.append({"kind": "command", "ts": pending["ts"], "score": None,
                           "device": "", "model": "", "heard": pending["text"]})
            pending = None

    for row in rows:
        message = row.get("message") or ""
        meta = row.get("meta") or {}

        if message.startswith("Heard: "):
            # Only one can be waiting; two transcripts in a row means the
            # older one never had a detection of its own.
            flush_pending()
            pending = {"ts": row["ts"], "text": message[len("Heard: "):]}
            continue

        if not message.startswith("Wake word heard"):
            continue

        heard = None
        if pending is not None:
            # Only if it plausibly followed this detection. A transcript
            # minutes later belongs to something else entirely.
            start, end = parsed(row["ts"]), parsed(pending["ts"])
            gap = (end - start).total_seconds() if (start and end) else 0
            if 0 <= gap <= COMMAND_PAIRING_WINDOW_S:
                heard = pending["text"]
                pending = None
            else:
                flush_pending()

        events.append({
            "kind": "detection",
            "ts": row["ts"],
            "score": meta.get("score"),
            "device": meta.get("device", ""),
            "model": meta.get("model", ""),
            "heard": heard,
        })
        if len(events) >= limit:
            break

    flush_pending()
    events.sort(key=lambda e: e["ts"], reverse=True)
    return events[:limit]


@router.get("/voice/log", tags=["voice"])
async def voice_log(limit: int = 40, user: Dict[str, Any] = Depends(current_user)):
    """What the microphone has actually heard.

    An always-on microphone should be able to show its work. The pieces were
    already being recorded -- a detection writes "Wake word heard" with its
    confidence, a transcription writes "Heard: ..." -- but they sat scattered
    through a general activity feed among schedule ticks and health checks,
    which is no use to someone asking "is it listening to me, and what does
    it think I said?"

    This pairs them up: each detection with the command that followed it, or
    an honest note that nothing did.
    """
    activity = container.get("activity")
    if activity is None:
        raise HTTPException(status_code=503, detail="The activity log is not available.")

    # Over-fetch: detections and transcripts interleave with everything else
    # the voice subsystem logs, so `limit` entries of source=voice is not
    # `limit` detections.
    rows = await activity.recent(limit=max(limit * 6, 120), source="voice")

    events = pair_voice_events(rows, limit)
    return {
        "events": events[:limit],
        "note": ("Each entry is a moment the wake word fired. 'heard' is what was "
                 "transcribed next, or null when nothing followed."),
    }
