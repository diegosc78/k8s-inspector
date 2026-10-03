#!/usr/bin/env python3
"""Adaptador OpenAI / Anthropic para la API de HolmesGPT.

Traduce  POST /v1/chat/completions  (OpenAI)  y  POST /v1/messages  (Anthropic)
a  POST /api/chat  de Holmes (SSE) y devuelve la respuesta en el formato pedido.

- Mientras Holmes investiga, emite indicadores de progreso (comandos ejecutados, razonamiento)
  y latidos periódicos, para que ni el usuario ni los proxies piensen que se ha colgado.
- Las peticiones auxiliares de Open WebUI (títulos, etiquetas, seguimientos, búsquedas, autocompletado) NO pasan por
  Holmes: se envían directamente al LLM subyacente (mismo MODEL), sin herramientas ni investigación.
- Recuerda las herramientas que Holmes consultó en turnos anteriores (caché del historial completo,
  localizada por el último par usuario/asistente) aunque el cliente solo reenvíe texto.

Variables de entorno: ver README ("API compatible con OpenAI / Anthropic").
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections import OrderedDict
from typing import Any, AsyncIterator, Optional

import httpx
import litellm
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

HOLMES_URL = os.environ.get("HOLMES_URL", "http://localhost:5050").rstrip("/")
HOLMES_API_KEY = os.environ.get("HOLMES_API_KEY", "").strip()
GATEWAY_API_KEY = os.environ.get("GATEWAY_API_KEY", "").strip()
GATEWAY_HOST = os.environ.get("GATEWAY_HOST", "0.0.0.0")
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "8080"))
PROGRESS_MODE = os.environ.get("PROGRESS_MODE", "reasoning").lower()  # reasoning | content | off
HEARTBEAT_SECONDS = float(os.environ.get("HEARTBEAT_SECONDS", "10"))
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "900"))
HISTORY_CACHE_MAX = int(os.environ.get("HISTORY_CACHE_MAX", "64"))
HISTORY_CACHE_TTL = float(os.environ.get("HISTORY_CACHE_TTL", "21600"))
DEFAULT_MODEL_ID = os.environ.get("DEFAULT_MODEL_ID", "holmes")
REASONING_MAX_CHARS = int(os.environ.get("REASONING_MAX_CHARS", "300"))
# Tareas auxiliares (Open WebUI) que se desvían al LLM sin pasar por Holmes. Expresiones regulares separadas por ";;"
# que se buscan en el último mensaje de usuario. TASK_PATTERNS sustituye a las de por defecto; TASK_PATTERNS_EXTRA las amplía.
DEFAULT_TASK_PATTERNS = [
    r"Generate a concise title summarizing the chat history",
    r"Generate 1-3 broad tags categorizing",
    r"Suggest 3-5 relevant follow-up questions",
    r"Analyze the chat history to determine the necessity of generating search queries",
    r"You are an autocompletion system",
    r"Generate a detailed prompt for am image generation",
]
TASK_BYPASS = os.environ.get("TASK_BYPASS", "true").lower() not in ("0", "false", "no")

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)-8s %(message)s")
log = logging.getLogger("holmes-gateway")
app = FastAPI(title="Holmes OpenAI/Anthropic gateway")


# ----------------------------------------------------------------------------- auth
@app.middleware("http")
async def auth(request: Request, call_next):
    if GATEWAY_API_KEY and request.url.path != "/healthz":
        key = request.headers.get("x-api-key", "")
        bearer = request.headers.get("authorization", "")
        if bearer.lower().startswith("bearer "):
            key = key or bearer[7:].strip()
        if key != GATEWAY_API_KEY:
            return JSONResponse({"error": {"message": "Invalid API key", "type": "authentication_error"}}, status_code=401)
    return await call_next(request)


# ----------------------------------------------------------------------------- memoria de herramientas
def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _pair_key(user: str, assistant: str) -> str:
    return hashlib.sha256(f"{_norm(user)}\x00{_norm(assistant)}".encode()).hexdigest()


class HistoryCache:
    """LRU + TTL del historial completo de Holmes (incluye tool_calls y resultados)."""

    def __init__(self, max_entries: int, ttl: float):
        self.max, self.ttl, self.data = max_entries, ttl, OrderedDict()

    def get(self, key: str) -> Optional[list]:
        item = self.data.get(key)
        if not item:
            return None
        if time.time() - item[0] > self.ttl:
            del self.data[key]
            return None
        self.data.move_to_end(key)
        return item[1]

    def put(self, key: str, history: list) -> None:
        if self.max <= 0:
            return
        self.data[key] = (time.time(), history)
        self.data.move_to_end(key)
        while len(self.data) > self.max:
            self.data.popitem(last=False)


cache = HistoryCache(HISTORY_CACHE_MAX, HISTORY_CACHE_TTL)


# ----------------------------------------------------------------------------- normalización de peticiones
def _text_and_images(content: Any) -> tuple[str, list]:
    """Admite str, o lista de bloques (OpenAI: text/image_url; Anthropic: text/image)."""
    if isinstance(content, str):
        return content, []
    texts, images = [], []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            texts.append(part.get("text", ""))
        elif kind == "image_url":
            url = part.get("image_url")
            images.append(url.get("url") if isinstance(url, dict) else url)
        elif kind == "image":
            src = part.get("source", {})
            if src.get("type") == "base64":
                images.append(f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}")
            elif src.get("type") == "url":
                images.append(src.get("url"))
    return "\n".join(t for t in texts if t), [i for i in images if i]


class ChatInput:
    def __init__(self, system: str, turns: list[tuple[str, str]], images: list, model: Optional[str]):
        self.system, self.turns, self.images, self.model = system, turns, images, model


def normalize(messages: list[dict], system_extra: str = "", model: Optional[str] = None) -> ChatInput:
    system = [system_extra] if system_extra else []
    turns: list[tuple[str, str]] = []
    images: list = []
    for m in messages:
        role = m.get("role")
        text, imgs = _text_and_images(m.get("content"))
        if role in ("system", "developer"):
            system.append(text)
        elif role in ("user", "assistant"):
            turns.append((role, text))
            images = imgs if role == "user" else images  # solo las imágenes del último turno de usuario
        # roles tool/function: los ejecuta Holmes por su cuenta, se ignoran
    return ChatInput("\n\n".join(s for s in system if s), turns, images, model)


async def holmes_models() -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{HOLMES_URL}/api/model", headers=_holmes_headers())
            return json.loads(r.json()["model_name"])
    except Exception as e:  # noqa: BLE001
        log.warning("No pude leer /api/model de Holmes: %s", e)
        return []


def _holmes_headers() -> dict:
    return {"X-API-Key": HOLMES_API_KEY} if HOLMES_API_KEY else {}


# ----------------------------------------------------------------------------- tareas auxiliares -> LLM directo
def _compile_patterns() -> list[re.Pattern]:
    raw = os.environ.get("TASK_PATTERNS")
    patterns = [p for p in raw.split(";;") if p.strip()] if raw else list(DEFAULT_TASK_PATTERNS)
    patterns += [p for p in os.environ.get("TASK_PATTERNS_EXTRA", "").split(";;") if p.strip()]
    return [re.compile(p, re.IGNORECASE) for p in patterns]


TASK_REGEXES = _compile_patterns()


def is_task(chat: ChatInput) -> Optional[str]:
    """Devuelve el patrón que coincide si la petición es una tarea auxiliar (no una pregunta para Holmes)."""
    if not TASK_BYPASS or not chat.turns:
        return None
    last = chat.turns[-1][1]
    for rx in TASK_REGEXES:
        if rx.search(last):
            return rx.pattern
    return None


async def direct_events(chat: ChatInput, pattern: str) -> AsyncIterator[tuple]:
    """Envía la petición tal cual al LLM subyacente (sin Holmes, sin herramientas)."""
    model = chat.model or os.environ.get("MODEL", "")
    if not model:
        names = await holmes_models()
        model = names[0] if names else ""
    if not model:
        yield ("error", "No hay modelo: define MODEL (el mismo que usa Holmes)")
        return
    messages = ([{"role": "system", "content": chat.system}] if chat.system else []) + [
        {"role": r, "content": t} for r, t in chat.turns
    ]
    started = time.monotonic()
    log.info("Tarea auxiliar -> LLM directo (%s), sin Holmes. Patrón: %r. Inicio: %.80r", model, pattern, chat.turns[-1][1][:80])
    try:
        resp = await litellm.acompletion(model=model, messages=messages, timeout=REQUEST_TIMEOUT, drop_params=True)
        usage = getattr(resp, "usage", None)
        log.info("Tarea auxiliar completada en %.1fs (tokens: %s)", time.monotonic() - started, getattr(usage, "total_tokens", "?"))
        yield ("answer", {"analysis": resp.choices[0].message.content or "",
                          "metadata": {"costs": {"prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                                                 "completion_tokens": getattr(usage, "completion_tokens", 0) or 0}}})
    except Exception as e:  # noqa: BLE001  (no se recurre a Holmes: lanzaría la investigación que queremos evitar)
        log.error("Tarea auxiliar fallida: %s: %s", type(e).__name__, e)
        yield ("error", f"{type(e).__name__}: {e}")


def events_for(chat: ChatInput) -> AsyncIterator[tuple]:
    pattern = is_task(chat)
    return direct_events(chat, pattern) if pattern else holmes_events(chat)


# ----------------------------------------------------------------------------- núcleo: Holmes -> eventos
def _progress_for(event: str, data: dict) -> Optional[str]:
    if event == "tool_calling_result":
        ok = (data.get("result") or {}).get("status") == "success"
        return f"{'🔧' if ok else '⚠️'} {data.get('description') or data.get('tool_name', 'herramienta')}"
    if event == "ai_message" and data.get("reasoning") and REASONING_MAX_CHARS > 0:
        text = _norm(data["reasoning"])
        return "💭 " + (text if len(text) <= REASONING_MAX_CHARS else text[: REASONING_MAX_CHARS] + "…")
    return None


async def holmes_events(chat: ChatInput) -> AsyncIterator[tuple]:
    """Genera ('progress', txt) | ('heartbeat', seg) | ('answer', {...}) | ('error', msg)."""
    prior = chat.turns[:-1]
    history = None
    if prior and prior[-1][0] == "assistant" and len(prior) >= 2 and prior[-2][0] == "user":
        history = cache.get(_pair_key(prior[-2][1], prior[-1][1]))
        log.info("Historial: %s", "recuperado de caché (con herramientas)" if history else "solo texto (sin caché)")
    if history is None and prior:
        # Holmes exige un mensaje system inicial (él lo sustituye por el suyo)
        history = [{"role": "system", "content": "."}] + [{"role": r, "content": t} for r, t in prior]
    ask = chat.turns[-1][1]
    body: dict = {"ask": ask, "stream": True}
    if history:
        body["conversation_history"] = history
    if chat.system:
        body["additional_system_prompt"] = chat.system
    if chat.images:
        body["images"] = chat.images
    if chat.model:
        body["model"] = chat.model

    queue: asyncio.Queue = asyncio.Queue()

    async def producer():
        try:
            timeout = httpx.Timeout(REQUEST_TIMEOUT, connect=10)
            async with httpx.AsyncClient(timeout=timeout) as c:
                async with c.stream("POST", f"{HOLMES_URL}/api/chat", json=body, headers=_holmes_headers()) as r:
                    if r.status_code != 200:
                        detail = (await r.aread()).decode(errors="replace")[:500]
                        await queue.put(("error", f"Holmes respondió HTTP {r.status_code}: {detail}"))
                        return
                    event, data = None, []
                    async for line in r.aiter_lines():
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif line == "" and event:
                            payload = json.loads("\n".join(data)) if data else {}
                            if event == "ai_answer_end":
                                cache.put(_pair_key(ask, payload.get("analysis") or ""), payload.get("conversation_history") or [])
                                await queue.put(("answer", payload))
                            elif event == "error":
                                await queue.put(("error", payload.get("msg") or payload.get("description") or "error de Holmes"))
                            elif (text := _progress_for(event, payload)):
                                await queue.put(("progress", text))
                            event, data = None, []
        except Exception as e:  # noqa: BLE001
            await queue.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            await queue.put(("done", None))

    task = asyncio.create_task(producer())
    started = time.monotonic()
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ("heartbeat", time.monotonic() - started)
                continue
            if item[0] == "done":
                return
            yield item
    finally:
        task.cancel()  # cliente desconectado: corta también la petición a Holmes


def _usage(payload: dict) -> tuple[int, int]:
    costs = (payload.get("metadata") or {}).get("costs") or (payload.get("metadata") or {}).get("usage") or {}
    return int(costs.get("prompt_tokens") or 0), int(costs.get("completion_tokens") or 0)


def _heartbeat_text(seconds: float) -> str:
    return f"⏳ Sigo trabajando… ({int(seconds)} s)"


def _sse(data: Any, event: Optional[str] = None) -> bytes:
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _pieces(text: str, size: int = 160) -> list[str]:
    """Trocea la respuesta final para que se vea fluir (Holmes no emite tokens sueltos)."""
    out, buf = [], ""
    for line in text.splitlines(keepends=True):
        buf += line
        if len(buf) >= size:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out or [text]


# ----------------------------------------------------------------------------- OpenAI
def _oa_error(msg: str, status: int = 502) -> JSONResponse:
    return JSONResponse({"error": {"message": msg, "type": "upstream_error", "code": None}}, status_code=status)


@app.get("/v1/models")
async def list_models():
    names = [DEFAULT_MODEL_ID] + [m for m in await holmes_models() if m != DEFAULT_MODEL_ID]
    return {"object": "list", "data": [{"id": n, "object": "model", "created": 0, "owned_by": "holmes"} for n in names]}


async def _resolve_model(requested: Optional[str]) -> Optional[str]:
    if not requested or requested == DEFAULT_MODEL_ID:
        return None
    return requested if requested in await holmes_models() else None


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    req = await request.json()
    messages = req.get("messages") or []
    chat = normalize(messages, model=await _resolve_model(req.get("model")))
    if not chat.turns or chat.turns[-1][0] != "user":
        return JSONResponse({"error": {"message": "El último mensaje debe ser del usuario", "type": "invalid_request_error"}}, status_code=400)
    model_name = req.get("model") or DEFAULT_MODEL_ID
    cid, created = f"chatcmpl-{uuid.uuid4().hex[:24]}", int(time.time())
    include_usage = bool((req.get("stream_options") or {}).get("include_usage"))

    def chunk(delta: dict, finish: Optional[str] = None, usage: Optional[dict] = None) -> bytes:
        body = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if usage is not None:
            body["usage"] = usage
        return _sse(body)

    if req.get("stream"):
        async def stream():
            yield chunk({"role": "assistant", "content": ""})
            progress_in_content = False
            async for kind, val in events_for(chat):
                if kind in ("progress", "heartbeat") and PROGRESS_MODE != "off":
                    text = val if kind == "progress" else _heartbeat_text(val)
                    if PROGRESS_MODE == "content":
                        progress_in_content = True
                        yield chunk({"content": f"> {text}\n"})
                    else:
                        yield chunk({"reasoning_content": text + "\n"})
                elif kind == "heartbeat":
                    yield b": keepalive\n\n"
                elif kind == "error":
                    yield chunk({"content": ("\n" if progress_in_content else "") + f"⚠️ Error: {val}"})
                    yield chunk({}, "stop")
                    yield b"data: [DONE]\n\n"
                    return
                elif kind == "answer":
                    if progress_in_content:
                        yield chunk({"content": "\n"})
                    for piece in _pieces(val.get("analysis") or ""):
                        yield chunk({"content": piece})
                    p, c = _usage(val)
                    yield chunk({}, "stop")
                    if include_usage:
                        body = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name, "choices": [],
                                "usage": {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}}
                        yield _sse(body)
                    yield b"data: [DONE]\n\n"
                    return
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # Sin streaming: espacios en blanco (JSON válido) como keepalive hasta tener la respuesta
    async def body_stream():
        result: dict = {"error": {"message": "Holmes no devolvió respuesta", "type": "upstream_error"}}
        async for kind, val in events_for(chat):
            if kind == "heartbeat":
                yield b" "
            elif kind == "error":
                result = {"error": {"message": val, "type": "upstream_error", "code": None}}
            elif kind == "answer":
                p, c = _usage(val)
                result = {"id": cid, "object": "chat.completion", "created": created, "model": model_name,
                          "choices": [{"index": 0, "message": {"role": "assistant", "content": val.get("analysis") or ""}, "finish_reason": "stop"}],
                          "usage": {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}}
        yield json.dumps(result, ensure_ascii=False).encode()
    return StreamingResponse(body_stream(), media_type="application/json")


# ----------------------------------------------------------------------------- Anthropic
@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    req = await request.json()
    system = req.get("system") or ""
    if isinstance(system, list):
        system = "\n".join(b.get("text", "") for b in system if isinstance(b, dict))
    chat = normalize(req.get("messages") or [], system_extra=system, model=await _resolve_model(req.get("model")))
    if not chat.turns or chat.turns[-1][0] != "user":
        return JSONResponse({"type": "error", "error": {"type": "invalid_request_error", "message": "El último mensaje debe ser del usuario"}}, status_code=400)
    model_name = req.get("model") or DEFAULT_MODEL_ID
    mid = f"msg_{uuid.uuid4().hex[:24]}"

    def message_obj(text: str, usage=(0, 0), done=False) -> dict:
        return {"id": mid, "type": "message", "role": "assistant", "model": model_name,
                "content": [{"type": "text", "text": text}] if done else [],
                "stop_reason": "end_turn" if done else None, "stop_sequence": None,
                "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}}

    if req.get("stream"):
        async def stream():
            yield _sse({"type": "message_start", "message": message_obj("")}, "message_start")
            index, thinking_open = 0, False
            async for kind, val in events_for(chat):
                if kind in ("progress", "heartbeat") and PROGRESS_MODE != "off":
                    text = val if kind == "progress" else _heartbeat_text(val)
                    if not thinking_open:
                        thinking_open = True
                        yield _sse({"type": "content_block_start", "index": index, "content_block": {"type": "thinking", "thinking": "", "signature": ""}}, "content_block_start")
                    yield _sse({"type": "content_block_delta", "index": index, "delta": {"type": "thinking_delta", "thinking": text + "\n"}}, "content_block_delta")
                elif kind == "heartbeat":
                    yield _sse({"type": "ping"}, "ping")
                elif kind in ("answer", "error"):
                    if thinking_open:
                        yield _sse({"type": "content_block_stop", "index": index}, "content_block_stop")
                        index += 1
                    text = (val.get("analysis") or "") if kind == "answer" else f"⚠️ Error: {val}"
                    p, c = _usage(val) if kind == "answer" else (0, 0)
                    yield _sse({"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}}, "content_block_start")
                    for piece in _pieces(text):
                        yield _sse({"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": piece}}, "content_block_delta")
                    yield _sse({"type": "content_block_stop", "index": index}, "content_block_stop")
                    yield _sse({"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"input_tokens": p, "output_tokens": c}}, "message_delta")
                    yield _sse({"type": "message_stop"}, "message_stop")
                    return
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def body_stream():
        result: dict = {"type": "error", "error": {"type": "api_error", "message": "Holmes no devolvió respuesta"}}
        async for kind, val in events_for(chat):
            if kind == "heartbeat":
                yield b" "
            elif kind == "error":
                result = {"type": "error", "error": {"type": "api_error", "message": val}}
            elif kind == "answer":
                result = message_obj(val.get("analysis") or "", _usage(val), done=True)
        yield json.dumps(result, ensure_ascii=False).encode()
    return StreamingResponse(body_stream(), media_type="application/json")


@app.get("/healthz")
async def healthz():
    return {"status": "healthy"}


if __name__ == "__main__":
    if not GATEWAY_API_KEY:
        log.warning("GATEWAY_API_KEY vacío: el gateway acepta peticiones sin autenticar")
    log.info("Gateway en %s:%s -> %s (progreso=%s, latido=%ss)", GATEWAY_HOST, GATEWAY_PORT, HOLMES_URL, PROGRESS_MODE, HEARTBEAT_SECONDS)
    uvicorn.run(app, host=GATEWAY_HOST, port=GATEWAY_PORT, log_level="warning")
