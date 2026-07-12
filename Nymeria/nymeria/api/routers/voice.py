"""Voice endpoints (STT, TTS, voice chat)."""

import io
import logging
import time as _time
from collections.abc import Callable
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from ...core.accounts import AuthenticatedUser

logger = logging.getLogger(__name__)


def create_voice_router(
    verify_api_key: Callable[..., Any],
    get_agent_fn: Callable[..., Any],
    get_settings_fn: Callable[..., Any],
    require_thread_access_fn: Callable[..., None],
) -> APIRouter:
    """Create the voice router with app dependencies injected."""
    router = APIRouter(tags=["Voice"])

    @router.post("/voice/chat")
    async def voice_chat(
        audio: UploadFile = File(..., description="Audio file (WAV, MP3, AAC, etc.)"),
        thread_id: Optional[str] = Form(default=None),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Voice conversation: audio in, audio out."""
        from ...core.voice import get_stt_service, get_tts_service, VoiceServiceError

        settings = get_settings_fn()

        tid = thread_id or settings.voice_default_thread_id or "watch-default"
        require_thread_access_fn(user, tid)

        try:
            stt = get_stt_service(settings)
            tts = get_tts_service(settings)
        except VoiceServiceError as e:
            raise HTTPException(status_code=503, detail=str(e))

        audio_bytes = await audio.read()
        if not audio_bytes:
            raise HTTPException(status_code=400, detail="Empty audio file")

        t0 = _time.monotonic()
        try:
            transcription = await stt.transcribe(
                audio_bytes,
                filename=audio.filename or "recording.wav",
                content_type=audio.content_type or "audio/wav",
            )
        except VoiceServiceError as e:
            raise HTTPException(status_code=502, detail=f"STT failed: {e}")
        stt_elapsed = _time.monotonic() - t0

        if not transcription:
            raise HTTPException(status_code=422, detail="Could not transcribe audio (empty result)")

        logger.info(f"[VOICE] STT ({stt_elapsed:.1f}s): {transcription[:100]}...")

        agent = get_agent_fn()

        # Global interactive-turn admission (backlog #83): a voice turn is an
        # interactive holder turn like any /chat message, so it draws against
        # the same ceiling (busy-thread prompts queue and pass through).
        from ...core.interactive_admission import (
            InteractiveCapacityError,
            admit_interactive_turn,
        )

        try:
            turn_slot = await admit_interactive_turn(agent, settings, tid)
        except InteractiveCapacityError as e:
            raise HTTPException(
                status_code=429,
                detail=e.detail,
                headers={"Retry-After": str(e.retry_after)},
            )

        t1 = _time.monotonic()
        try:
            response_text = ""
            async for event in agent.astream(
                transcription, thread_id=tid, user_id=user.id,
                _trigger_override=(
                    "Smartwatch — respond concisely (1-2 sentences max), "
                    "your reply will be spoken aloud via TTS"
                ),
            ):
                if event.get("type") == "response":
                    response_text += event.get("content", "")
                elif event.get("type") == "error":
                    raise Exception(event.get("content", "Unknown agent error"))
        except Exception as e:
            logger.error(f"[VOICE] Agent error: {e}")
            raise HTTPException(status_code=500, detail=f"Agent error: {e}")
        finally:
            if turn_slot is not None:
                turn_slot.release()
        agent_elapsed = _time.monotonic() - t1

        if not response_text:
            response_text = "I received your message but had no response."

        logger.info(f"[VOICE] Agent ({agent_elapsed:.1f}s): {response_text[:100]}...")

        t2 = _time.monotonic()
        try:
            audio_out, content_type = await tts.synthesize(response_text)
        except VoiceServiceError as e:
            raise HTTPException(status_code=502, detail=f"TTS failed: {e}")
        tts_elapsed = _time.monotonic() - t2

        logger.info(f"[VOICE] TTS ({tts_elapsed:.1f}s): {len(audio_out)} bytes")
        logger.info(f"[VOICE] Total pipeline: STT={stt_elapsed:.1f}s + Agent={agent_elapsed:.1f}s + TTS={tts_elapsed:.1f}s = {stt_elapsed+agent_elapsed+tts_elapsed:.1f}s")

        return StreamingResponse(
            io.BytesIO(audio_out),
            media_type=content_type,
            headers={"Cache-Control": "no-transform"},
        )

    @router.post("/voice/tts")
    async def voice_tts(
        request: Request,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Text-to-Speech: accepts JSON {text: string, voice_note?: bool}, returns audio bytes.

        ``voice_note: true`` asks for a container chat platforms accept as a
        voice message (Ogg/Opus or MP3); the response Content-Type tells the
        caller what it got.
        """
        from ...core.voice import get_tts_service, VoiceServiceError

        body = await request.json()
        text = body.get("text", "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="'text' field is required")
        voice_note = body.get("voice_note") is True

        settings = get_settings_fn()
        try:
            tts = get_tts_service(settings)
        except VoiceServiceError as e:
            raise HTTPException(status_code=503, detail=str(e))

        try:
            audio_bytes, content_type = await tts.synthesize(text, voice_note=voice_note)
        except VoiceServiceError as e:
            raise HTTPException(status_code=502, detail=f"TTS failed: {e}")

        return Response(content=audio_bytes, media_type=content_type)

    @router.post("/voice/stt")
    async def voice_stt(
        audio: UploadFile = File(..., description="Audio file to transcribe"),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Speech-to-Text: accepts audio file upload, returns JSON {text: string}."""
        from ...core.voice import get_stt_service, VoiceServiceError

        settings = get_settings_fn()
        try:
            stt = get_stt_service(settings)
        except VoiceServiceError as e:
            raise HTTPException(status_code=503, detail=str(e))

        audio_bytes = await audio.read()
        if not audio_bytes:
            raise HTTPException(status_code=400, detail="Empty audio file")

        try:
            text = await stt.transcribe(
                audio_bytes,
                filename=audio.filename or "recording.wav",
                content_type=audio.content_type or "audio/wav",
            )
        except VoiceServiceError as e:
            raise HTTPException(status_code=502, detail=f"STT failed: {e}")

        return {"text": text}

    return router
