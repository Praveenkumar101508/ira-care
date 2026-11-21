"""
Feature handler registry for chat_stream.  # Fix P24

Each handler exposes:
  matches(req) -> bool   — return True if this handler owns the request
  handle(req, user) -> EventSourceResponse  — execute and return the SSE response

Handlers are checked in order; the first match wins. Architect routing and the
main LLM event_generator remain in chat.py (tightly coupled to local state).
"""
from __future__ import annotations

import asyncio
import json
from typing import Protocol

from fastapi import HTTPException
from sse_starlette.sse import EventSourceResponse

from api.routes.chat import ChatRequest
from config import get_settings


class FeatureHandler(Protocol):
    def matches(self, req: "ChatRequest") -> bool: ...
    async def handle(self, req: "ChatRequest", user: str) -> "EventSourceResponse": ...


# ── Image Generation ──────────────────────────────────────────────────────────

class ImageGenHandler:
    def matches(self, req: ChatRequest) -> bool:
        if req.is_voice:
            return False
        from utils.search_tools import is_image_gen_request
        return is_image_gen_request(req.message)

    async def handle(self, req: ChatRequest, user: str) -> EventSourceResponse:
        async def _stream():
            yield {"data": json.dumps({"token": "Generating your image… "})}
            try:
                from api.routes.image_gen import GenerateRequest, _generate_sd_webui, _generate_replicate
                _cfg = get_settings()
                _url = _cfg.image_gen_url.rstrip("/")
                _tok = _cfg.replicate_api_token
                if _url or _tok:
                    gen_req = GenerateRequest(prompt=req.message, width=1024, height=1024, steps=20)
                    image_b64 = (
                        await _generate_sd_webui(gen_req, _url) if _url
                        else await _generate_replicate(gen_req, _tok, _cfg.flux_model)
                    )
                    yield {"data": json.dumps({
                        "image_generated": True, "image_b64": image_b64,
                        "mime_type": "image/png", "prompt": req.message,
                    })}
                    final_text = f"Here is your generated image based on: *{req.message}*"
                else:
                    final_text = (
                        "Image generation is not configured yet.\n\n"
                        "To enable it, add one of these to your `.env`:\n"
                        "- `IMAGE_GEN_URL=http://your-sd-webui:7860` (Stable Diffusion)\n"
                        "- `REPLICATE_API_TOKEN=r8_...` (Flux via Replicate)\n\n"
                        "Once configured, just ask me to *generate* or *draw* something and I will."
                    )
            except HTTPException as e:
                # e.g. the external gate refusing while IRA_ALLOW_EXTERNAL_API is off —
                # surface the reason verbatim instead of a bare status code.
                final_text = str(e.detail)
            except Exception as e:
                final_text = f"Image generation failed: {e}"
            for word in final_text.split(" "):
                yield {"data": json.dumps({"token": word + " "})}
                await asyncio.sleep(0.01)
            yield {"data": json.dumps({
                "done": True, "agent": "image_gen",
                "latency_ms": 0, "session_id": req.session_id,
            })}
        return EventSourceResponse(_stream())


# ── Image Edit ────────────────────────────────────────────────────────────────

class ImageEditHandler:
    def matches(self, req: ChatRequest) -> bool:
        if not req.image_b64:
            return False
        from utils.search_tools import is_image_edit_request
        return is_image_edit_request(req.message)

    async def handle(self, req: ChatRequest, user: str) -> EventSourceResponse:
        async def _stream():
            yield {"data": json.dumps({"token": "Editing your image… "})}
            try:
                from api.routes.image_gen import EditRequest, _edit_sd_webui, _edit_replicate
                _cfg = get_settings()
                _url = _cfg.image_gen_url.rstrip("/")
                _tok = _cfg.replicate_api_token
                if _url or _tok:
                    edit_req = EditRequest(image_b64=req.image_b64, instruction=req.message)
                    image_b64 = (
                        await _edit_sd_webui(edit_req, _url) if _url
                        else await _edit_replicate(edit_req, _tok)
                    )
                    yield {"data": json.dumps({
                        "image_generated": True, "image_b64": image_b64,
                        "mime_type": "image/png", "prompt": req.message,
                    })}
                    final_text = "Here is the edited image."
                else:
                    final_text = "Image editing requires IMAGE_GEN_URL or REPLICATE_API_TOKEN in .env."
            except HTTPException as e:
                final_text = str(e.detail)
            except Exception as e:
                final_text = f"Image editing failed: {e}"
            for word in final_text.split(" "):
                yield {"data": json.dumps({"token": word + " "})}
                await asyncio.sleep(0.01)
            yield {"data": json.dumps({
                "done": True, "agent": "image_edit",
                "latency_ms": 0, "session_id": req.session_id,
            })}
        return EventSourceResponse(_stream())


# ── Video Generation ──────────────────────────────────────────────────────────

class VideoGenHandler:
    def matches(self, req: ChatRequest) -> bool:
        if req.is_voice:
            return False
        from api.routes.video_gen import is_video_gen_request
        return is_video_gen_request(req.message)

    async def handle(self, req: ChatRequest, user: str) -> EventSourceResponse:
        from api.routes.video_gen import video_generate, VideoGenRequest
        return await video_generate(VideoGenRequest(prompt=req.message, session_id=req.session_id), user)


# ── Document Creation ─────────────────────────────────────────────────────────

class DocCreateHandler:
    def matches(self, req: ChatRequest) -> bool:
        if req.is_voice:
            return False
        from api.routes.document_create import is_doc_create_request
        return is_doc_create_request(req.message)

    async def handle(self, req: ChatRequest, user: str) -> EventSourceResponse:
        from api.routes.document_create import document_create, DocCreateRequest
        return await document_create(DocCreateRequest(prompt=req.message, session_id=req.session_id), user)


# ── Design Tools ──────────────────────────────────────────────────────────────

class DesignHandler:
    def matches(self, req: ChatRequest) -> bool:
        if req.is_voice:
            return False
        from api.routes.design_tools import is_design_request
        return is_design_request(req.message)

    async def handle(self, req: ChatRequest, user: str) -> EventSourceResponse:
        from api.routes.design_tools import design_generate, DesignRequest
        return await design_generate(DesignRequest(prompt=req.message, session_id=req.session_id), user)


# ── Audio Generation ──────────────────────────────────────────────────────────

class AudioGenHandler:
    def matches(self, req: ChatRequest) -> bool:
        if req.is_voice:
            return False
        from api.routes.audio_gen import is_audio_gen_request
        return is_audio_gen_request(req.message)

    async def handle(self, req: ChatRequest, user: str) -> EventSourceResponse:
        from api.routes.audio_gen import audio_generate, AudioGenRequest
        return await audio_generate(AudioGenRequest(prompt=req.message, session_id=req.session_id), user)


# ── Deep Research ─────────────────────────────────────────────────────────────

class DeepResearchHandler:
    def matches(self, req: ChatRequest) -> bool:
        if req.is_voice:
            return False
        from api.routes.deep_research import is_deep_research_request
        return is_deep_research_request(req.message)

    async def handle(self, req: ChatRequest, user: str) -> EventSourceResponse:
        from api.routes.deep_research import deep_research, DeepResearchRequest
        return await deep_research(DeepResearchRequest(topic=req.message, session_id=req.session_id), user)


# ── Article Generation ────────────────────────────────────────────────────────

class ArticleHandler:
    def matches(self, req: ChatRequest) -> bool:
        if req.is_voice:
            return False
        from api.routes.deep_research import is_article_request
        return is_article_request(req.message)

    async def handle(self, req: ChatRequest, user: str) -> EventSourceResponse:
        from api.routes.deep_research import generate_article, ArticleRequest
        return await generate_article(ArticleRequest(topic=req.message, session_id=req.session_id), user)


# ── Interview preparation ─────────────────────────────────────────────────────

class InterviewPrepHandler:
    """Answer interview-prep questions from the local question bank.

    The rows are looked up before the model is called and passed in as reference
    data, so the companies, difficulties and frequencies in the answer come from
    the dataset rather than from a model's recollection of LeetCode discussion.
    """

    def matches(self, req: ChatRequest) -> bool:
        if req.is_voice:
            return False
        from interview.chat import is_interview_prep_request
        return is_interview_prep_request(req.message)

    async def handle(self, req: ChatRequest, user: str) -> EventSourceResponse:
        from interview import dataset as ds
        from interview.chat import SYSTEM_PROMPT, gather_context, render_context

        async def _stream():
            try:
                context = await asyncio.to_thread(gather_context, req.message)
                reference = render_context(context)
            except ds.DatasetUnavailable as exc:
                yield {"data": json.dumps({"token": str(exc)})}
                yield {"data": json.dumps({"done": True})}
                return
            except Exception as exc:  # noqa: BLE001 — fall back to a plain answer
                yield {"data": json.dumps({
                    "token": f"The question bank could not be read ({str(exc)[:120]}).",
                })}
                yield {"data": json.dumps({"done": True})}
                return

            yield {"data": json.dumps({
                "source": "interview_bank",
                "snapshot": context.get("stats", {}).get("snapshot"),
                "kind": context.get("kind"),
            })}

            from utils.llm import stream_tokens

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"{reference}\n\nTHEY ASKED: {req.message}"},
            ]
            try:
                async for token in stream_tokens(messages, use_deep=False, max_tokens=1200):
                    yield {"data": json.dumps({"token": token})}
            except Exception as exc:  # noqa: BLE001 — the rows are still useful
                yield {"data": json.dumps({
                    "token": f"\n\n(The model was unavailable: {str(exc)[:120]}. "
                             f"The raw list is at /api/v1/interview.)",
                })}
            yield {"data": json.dumps({"done": True})}

        return EventSourceResponse(_stream())


# ── Ordered registry — first match wins ──────────────────────────────────────
#
# Order matters: image_edit before image_gen (image_edit requires image_b64
# AND an edit keyword; image_gen is broader). video before doc/design/audio
# to avoid overlap.
HANDLER_REGISTRY: list[FeatureHandler] = [
    ImageEditHandler(),
    ImageGenHandler(),
    VideoGenHandler(),
    DocCreateHandler(),
    DesignHandler(),
    AudioGenHandler(),
    DeepResearchHandler(),
    ArticleHandler(),
    # Last: its detection is the narrowest, and a message that mentions a company
    # while asking for an image is an image request first.
    InterviewPrepHandler(),
]


async def dispatch(req: ChatRequest, user: str):
    """
    Iterate the handler registry; return the first matching handler's response,
    or None if no handler matches (caller falls through to main LLM stream).
    """
    for handler in HANDLER_REGISTRY:
        if handler.matches(req):
            return await handler.handle(req, user)
    return None
