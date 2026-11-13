"""Live model/voice catalogs, fetched from each provider's API.

Populates the config form's model + voice dropdowns with the actual, current
options instead of a hand-maintained list. Keys come from the Workspace's
Integration when it has one (#56), so the pickers list what the account that will
run the call actually has; otherwise from the builder's own env (OPENAI_API_KEY,
ANTHROPIC_API_KEY, ELEVENLABS_API_KEY, CARTESIA_API_KEY, DEEPGRAM_API_KEY,
GOOGLE_API_KEY, ASSEMBLYAI_API_KEY, XAI_API_KEY, HEYGEN_LIVE_AVATAR_API_KEY,
TAVUS_API_KEY;
OpenRouter's model list is public). Best-effort: a provider
with no key / no list API / an error yields [] (the console still allows free
text), and a stale cache is served over a transient failure. Cached ~1h.
Deepgram's Flux models are the exception: its list never names them, so they
are always offered.

AWS is the exception: listing Bedrock models needs signed SigV4 credentials the
builder has no reason to hold (they belong to the TurnCall runtime), so its
catalogs are short curated lists. Model access is per-account and per-region
anyway, so no list would be accurate for every user — the console allows free
text, which is what the ARN and inference-profile forms need.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger("turncall_builder")

_CACHE: dict[str, tuple[float, list]] = {}
_TTL = 3600.0

# OpenAI TTS has a fixed voice set with no list API.
_OPENAI_TTS_VOICES = [
    "alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse",
]


async def _cached(key: str, coro_fn) -> list:
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            values = await coro_fn(client)
    except Exception as exc:  # noqa: BLE001 — never fail the request over a catalog
        logger.warning("provider catalog fetch failed (%s): %s", key, exc)
        values = hit[1] if hit else []  # serve stale on a transient error
    _CACHE[key] = (now + _TTL, values)
    return values


# STT model sets are small and fixed per provider (only Deepgram has a list API).
_STT_MODELS = {
    "openai": ["whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe"],
    "elevenlabs": ["scribe_v1"],
    "cartesia": ["ink-whisper"],
    # Universal-Streaming English is TurnCall's default; the multilingual sibling
    # costs the same; Universal-3 Pro is 3x the price (turncall#203).
    "assemblyai": [
        "universal-streaming-english",
        "universal-streaming-multilingual",
        "universal-3-5-pro",
        "universal-3-6-pro",
    ],
    # TurnCall's default; xAI has no list endpoint for its transcription models.
    "xai": ["grok-voice-transcribe-2.0"],
}
# TTS model sets are small and fixed, so they are listed rather than fetched.
# Deepgram has none: its voice *is* its model, so the form shows no model field.
_TTS_MODELS = {
    "openai": ["tts-1", "tts-1-hd", "gpt-4o-mini-tts"],
    "elevenlabs": [
        "eleven_flash_v2_5", "eleven_turbo_v2_5", "eleven_multilingual_v2",
        "eleven_flash_v2", "eleven_turbo_v2", "eleven_v3_conversational",
    ],
    "cartesia": ["sonic-3.6", "sonic-3", "sonic-2"],
}
# Deepgram Flux (turncall#204): a model on the deepgram provider that Deepgram's
# /v1/models does not list (checked 2026-10-09), so it is added by hand.
_DEEPGRAM_FLUX = ["flux-general-en", "flux-general-multi"]

# S2S (speech-to-speech / realtime). OpenAI models are fetched live; its voice set
# is fixed (a subset of the TTS voices). Gemini Live has no key here + a large,
# growing voice set, so its models are a short fixed list and voices are free text.
_OPENAI_S2S_VOICES = ["alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse"]
# Fallback when the account's /v1/models list cannot be fetched. The gpt-4o
# previews that used to live here are retired — OpenAI no longer serves them,
# so offering them produced model_not_found at call time.
_OPENAI_S2S_MODELS = ["gpt-realtime-2.1", "gpt-realtime-2.1-mini", "gpt-realtime"]
# gpt-live-1 is full duplex and speaks a different protocol from Realtime
# (/v1/live/sessions, not /v1/realtime), so it is its own provider rather than
# another model under `openai`.
_OPENAI_LIVE_MODELS = ["gpt-live-1"]
# Fallback when no GOOGLE_API_KEY is set to fetch the live model list.
_GOOGLE_S2S_MODELS = [
    "gemini-3.1-flash-live-preview",
    "gemini-2.5-flash-native-audio-preview-09-2025",
    "gemini-live-2.5-flash-preview",
    "gemini-2.0-flash-live-001",
]
# Gemini Live has no voice-list API — this is its published native-audio voice set.
_GOOGLE_S2S_VOICES = [
    "Aoede", "Charon", "Fenrir", "Kore", "Leda", "Orus", "Puck", "Zephyr",
    "Achernar", "Achird", "Algenib", "Algieba", "Alnilam", "Autonoe", "Callirrhoe",
    "Despina", "Enceladus", "Erinome", "Gacrux", "Iapetus", "Laomedeia", "Pulcherrima",
    "Rasalgethi", "Sadachbia", "Sadaltager", "Schedar", "Sulafat", "Umbriel",
    "Vindemiatrix", "Zubenelgenubi",
]


# AWS Bedrock: curated, not fetched (see module docstring). Cross-region
# inference profiles ("us."/"eu." prefixes) fail over within a geography and are
# usually the right default in production; direct ids and provisioned-throughput
# ARNs also work and can be typed in freely.
_BEDROCK_MODELS = [
    # Anthropic entries are the "us." cross-region inference profiles on
    # purpose: newer Anthropic models reject their own bare id with
    # "Invocation ... with on-demand throughput isn't supported". Outside the
    # US the prefix is "eu." / "apac." — the console allows free text for that.
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-opus-4-6-v1",
    # Amazon's own models invoke fine by bare id.
    "amazon.nova-2-lite-v1:0",
    "amazon.nova-pro-v1:0",
    "meta.llama3-3-70b-instruct-v1:0",
]

# Amazon Nova Sonic. 2 is the default; v1 is the older model and does not
# support endpointing_sensitivity.
_AWS_S2S_MODELS = ["amazon.nova-2-sonic-v1:0", "amazon.nova-sonic-v1:0"]
_AWS_S2S_VOICES = ["matthew", "tiffany", "amy", "lupe", "carlos"]

# Grok Voice (turncall#384). Curated, not fetched: grok-voice-3.0 is what
# TurnCall runs when none is named (pinned, never the moving
# `grok-voice-latest`). xAI's deprecated 2.0 is not offered, as no picker
# offers a deprecated model. Voices are the account's own, custom ones
# included, from xAI's voice list.
_XAI_S2S_MODELS = ["grok-voice-3.0"]
# Eleven Realtime (turncall#465). Its session takes these named voices (from
# ElevenLabs' own error listing them) or any voice id the account can use.
_ELEVENLABS_S2S_MODELS = ["eleven_realtime_v1_mini", "eleven_realtime_v1_max"]
_ELEVENLABS_S2S_VOICES = ["brian", "chris", "george", "jessica", "liam", "lily", "matilda", "michael", "sarah", "will"]
# xai as a cascade LLM (turncall#385): TurnCall's default, offered whenever the
# account's own list cannot be had — no key, or a voice-only key xAI refuses
# on its chat endpoints (403).
_XAI_LLM_DEFAULT = "grok-4.20-non-reasoning"
# xAI's /v1/models lists image, video and voice models beside the chat ones.
_XAI_NOT_CHAT = ("image", "imagine", "video", "voice", "tts", "transcribe", "embed")


# The Builder's own environment: the fallback when a Workspace has no Integration.
_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "cartesia": "CARTESIA_API_KEY",
    "assemblyai": "ASSEMBLYAI_API_KEY",
    "xai": "XAI_API_KEY",
    # The same names TurnCall reads for the video avatars.
    "heygen": "HEYGEN_LIVE_AVATAR_API_KEY",
    "tavus": "TAVUS_API_KEY",
}
_ACCOUNT = {"openai_live": "openai"}


def _key(provider: str, keys: dict[str, str] | None) -> str:
    """The Workspace's key for this provider's account, else the Builder's own (#56)."""
    account = _ACCOUNT.get(provider, provider)
    return (keys or {}).get(account) or os.environ.get(_ENV.get(account, ""), "")


def _fp(key: str) -> str:
    """Cache-key fragment naming WHICH key fetched a list, never the key itself.

    Keying the cache by the credential is what invalidates it: a Workspace that
    connects, replaces or removes a key reads under a different fingerprint, so
    its pickers repopulate immediately rather than an hour later, and two
    Workspaces never share a list fetched with one of their keys.
    """
    return hashlib.sha256(key.encode()).hexdigest()[:16] if key else "none"


async def llm_models(provider: str, keys: dict[str, str] | None = None) -> list[str]:
    key = _key(provider, keys)
    models = await _cached(f"llm:{provider}:{_fp(key)}", lambda c: _fetch_llm_models(c, provider, key))
    if provider == "xai":  # after the cache, so a failed fetch still offers the default
        return models or [_XAI_LLM_DEFAULT]
    return models


async def tts_voices(provider: str, keys: dict[str, str] | None = None) -> list[dict[str, str]]:
    key = _key(provider, keys)
    return await _cached(f"tts:{provider}:{_fp(key)}", lambda c: _fetch_tts_voices(c, provider, key))


def tts_models(provider: str) -> list[str]:
    return _TTS_MODELS.get(provider, [])


async def stt_models(provider: str, keys: dict[str, str] | None = None) -> list[str]:
    key = _key(provider, keys)
    models = await _cached(f"stt:{provider}:{_fp(key)}", lambda c: _fetch_stt_models(c, provider, key))
    if provider == "deepgram":  # after the cache, so a failed fetch keeps Flux
        return sorted(set(models) | set(_DEEPGRAM_FLUX))
    return models


async def s2s_models(provider: str, keys: dict[str, str] | None = None) -> list[str]:
    key = _key(provider, keys)
    return await _cached(f"s2s:{provider}:{_fp(key)}", lambda c: _fetch_s2s_models(c, provider, key))


async def s2s_voices(provider: str, keys: dict[str, str] | None = None) -> list[dict[str, str]]:
    key = _key(provider, keys)
    return await _cached(f"s2sv:{provider}:{_fp(key)}", lambda c: _fetch_s2s_voices(c, provider, key))


async def _fetch_llm_models(client: httpx.AsyncClient, provider: str, key: str) -> list[str]:
    if provider == "openrouter":
        r = await client.get("https://openrouter.ai/api/v1/models")
        r.raise_for_status()
        return sorted(m["id"] for m in r.json().get("data", []) if m.get("id"))
    if provider == "openai":
        if not key:
            return []
        r = await client.get(
            "https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}
        )
        r.raise_for_status()
        ids = _live_ids(r.json())
        # "instruct": completions-only models (no chat/responses support).
        skip = ("audio", "realtime", "transcribe", "tts", "image", "embedding", "moderation", "dall-e", "instruct")
        return sorted(
            i for i in ids
            if i.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")) and not any(x in i for x in skip)
        )
    if provider == "anthropic":
        if not key:
            return []
        r = await client.get(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        )
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", []) if m.get("id")]
    if provider == "google":
        if not key:
            return []
        # Gemini chat models only: Gemma/AQA and the TTS, image and audio
        # Geminis also answer generateContent but are not a cascade LLM.
        skip = ("live", "audio", "tts", "image", "embedding")
        return [
            m for m in await _google_models(client, key, "generateContent")
            if m.startswith("gemini") and not any(x in m for x in skip)
        ]
    if provider == "bedrock":
        return list(_BEDROCK_MODELS)
    if provider == "xai":
        if not key:
            return []
        r = await client.get("https://api.x.ai/v1/models", headers={"Authorization": f"Bearer {key}"})
        r.raise_for_status()
        ids = [m["id"] for m in r.json().get("data", []) if isinstance(m, dict) and m.get("id")]
        return sorted(i for i in ids if i.startswith("grok") and not any(x in i for x in _XAI_NOT_CHAT))
    return []  # ollama / custom_openai target a per-agent endpoint


async def _fetch_s2s_models(client: httpx.AsyncClient, provider: str, key: str) -> list[str]:
    if provider == "openai":
        if not key:
            return list(_OPENAI_S2S_MODELS)  # no key — best-effort known set
        r = await client.get(
            "https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}
        )
        r.raise_for_status()
        ids = _live_ids(r.json())
        # Only the account's actual realtime models — injecting known-but-
        # unavailable ids (e.g. deprecated gpt-4o previews) fails at call time
        # with model_not_found. "realtime" also matches transcription/translation
        # models the *dialog* endpoint rejects, so drop those.
        skip = ("whisper", "translate", "transcribe")
        dialog = sorted(i for i in ids if "realtime" in i and not any(x in i for x in skip))
        return dialog or list(_OPENAI_S2S_MODELS)  # fallback if the list came back bare
    if provider == "openai_live":
        if not key:
            return list(_OPENAI_LIVE_MODELS)
        r = await client.get(
            "https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}
        )
        r.raise_for_status()
        ids = _live_ids(r.json())
        # "live" also matches gpt-live-transcribe, a transcription model the
        # dialog endpoint rejects — same reason the realtime branch drops
        # gpt-realtime-whisper.
        skip = ("whisper", "translate", "transcribe")
        dialog = sorted(i for i in ids if "live" in i and not any(x in i for x in skip))
        return dialog or list(_OPENAI_LIVE_MODELS)
    if provider == "google":
        live = await _google_models(client, key, "bidiGenerateContent") if key else []
        return live or list(_GOOGLE_S2S_MODELS)  # no key / empty — published fallback
    if provider == "aws":
        return list(_AWS_S2S_MODELS)
    if provider == "xai":
        return list(_XAI_S2S_MODELS)
    if provider == "elevenlabs":
        return list(_ELEVENLABS_S2S_MODELS)
    return []


async def _google_models(client: httpx.AsyncClient, key: str, method: str) -> list[str]:
    """The account's Gemini API models supporting `method`, sorted."""
    r = await client.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        headers={"x-goog-api-key": key},
        params={"pageSize": 1000},  # the API's maximum; ponytail: no nextPageToken, a few hundred models today
    )
    r.raise_for_status()
    return sorted(
        m["name"].removeprefix("models/")
        for m in r.json().get("models", [])
        if method in (m.get("supportedGenerationMethods") or [])
    )


def _live_ids(listing: dict[str, Any]) -> list[str]:
    """The ids in OpenAI's model list that it has not deprecated: a model with
    a `shutdown_date` is on its way out (or already answers 404), so no picker
    offers it. An agent already on one keeps it; the picker shows its value."""
    return [
        m["id"] for m in listing.get("data", [])
        if m.get("id") and m.get("shutdown_date") is None
    ]


def _pair(value: str, label: str) -> dict[str, str]:
    return {"value": value, "label": label}


def _same(values: list[str]) -> list[dict[str, str]]:
    """Voices whose config value IS the display string (OpenAI, Google)."""
    return [_pair(v, v) for v in values]


async def _fetch_s2s_voices(client: httpx.AsyncClient, provider: str, key: str) -> list[dict[str, str]]:
    if provider == "openai":
        return _same(_OPENAI_S2S_VOICES)
    if provider == "openai_live":
        # GPT-Live-1 adds voices across more accents and languages, but OpenAI
        # publishes no list API for them. The realtime set is the subset we can
        # name with confidence.
        return _same(_OPENAI_S2S_VOICES)
    if provider == "google":
        return _same(_GOOGLE_S2S_VOICES)
    if provider == "aws":
        return _same(_AWS_S2S_VOICES)
    if provider == "xai":
        return await _xai_voices(client, key)
    if provider == "elevenlabs":
        account = await _fetch_tts_voices(client, provider, key) if key else []
        return _same(_ELEVENLABS_S2S_VOICES) + account
    return []


async def _xai_voices(client: httpx.AsyncClient, key: str) -> list[dict[str, str]]:
    """The account's voices, custom ones included: Grok Voice (S2S) and xai TTS
    speak with the same set."""
    if not key:
        return []
    r = await client.get("https://api.x.ai/v1/tts/voices", headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    pairs = [
        _pair(v["voice_id"], v.get("name") or v["voice_id"])
        for v in r.json().get("voices", [])
        if isinstance(v, dict) and v.get("voice_id")
    ]
    return sorted(pairs, key=lambda p: p["label"])


async def _fetch_stt_models(client: httpx.AsyncClient, provider: str, key: str) -> list[str]:
    if provider == "deepgram":
        if not key:
            return []
        r = await client.get(
            "https://api.deepgram.com/v1/models", headers={"Authorization": f"Token {key}"}
        )
        r.raise_for_status()
        names = [m.get("canonical_name") or m.get("name") for m in r.json().get("stt", [])]
        return sorted({n for n in names if n})  # dedup: Deepgram lists per-version/language rows
    return _STT_MODELS.get(provider, [])


async def _fetch_tts_voices(client: httpx.AsyncClient, provider: str, key: str) -> list[dict[str, str]]:
    # ElevenLabs + Cartesia need a voice *id* in config (the display name would
    # build a broken TTS websocket URL -> "did not receive a valid HTTP
    # response"), so value=id, label=name. Deepgram/OpenAI use the string as-is.
    if provider == "openai":
        return _same(_OPENAI_TTS_VOICES)
    if provider == "elevenlabs":
        if not key:
            return []
        r = await client.get("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": key})
        r.raise_for_status()
        pairs = [
            _pair(v["voice_id"], v.get("name") or v["voice_id"])
            for v in r.json().get("voices", [])
            if v.get("voice_id")
        ]
        return sorted(pairs, key=lambda p: p["label"])
    if provider == "cartesia":
        if not key:
            return []
        r = await client.get(
            "https://api.cartesia.ai/voices",
            headers={"X-API-Key": key, "Cartesia-Version": "2024-06-10"},
        )
        r.raise_for_status()
        data = r.json()
        items = data if isinstance(data, list) else data.get("data", [])
        pairs = [
            _pair(v["id"], v.get("name") or v["id"])
            for v in items
            if isinstance(v, dict) and v.get("id")
        ]
        return sorted(pairs, key=lambda p: p["label"])
    if provider == "deepgram":
        if not key:
            return []
        r = await client.get(
            "https://api.deepgram.com/v1/models", headers={"Authorization": f"Token {key}"}
        )
        r.raise_for_status()
        names = [m.get("canonical_name") or m.get("name") for m in r.json().get("tts", [])]
        return _same(sorted({n for n in names if n}))  # dedup per-version/language rows
    if provider == "xai":
        return await _xai_voices(client, key)
    return []


# --- Avatars (#126) ------------------------------------------------------------

AVATAR_PROVIDERS = ("heygen", "tavus")

# The picker lists these and a saved HeyGen key is checked against them (_PROBES).
_LIVEAVATAR_AVATARS = "https://api.liveavatar.com/v1/avatars"


def _liveavatar_headers(key: str) -> dict[str, str]:
    return {"X-API-KEY": key}


async def avatars(provider: str, keys: dict[str, str] | None = None) -> list[dict[str, str]]:
    """The account's Avatars as {value: the ID TurnCall takes, label: its name}."""
    key = _key(provider, keys)
    return await _cached(f"avatar:{provider}:{_fp(key)}", lambda c: _fetch_avatars(c, provider, key))


async def _fetch_avatars(client: httpx.AsyncClient, provider: str, key: str) -> list[dict[str, str]]:
    if not key:
        return []
    if provider == "heygen":
        return await _liveavatar_avatars(client, key)
    if provider == "tavus":
        return await _tavus_faces(client, key)
    return []


async def _liveavatar_avatars(client: httpx.AsyncClient, key: str) -> list[dict[str, str]]:
    """The account's own avatars, then LiveAvatar's stock ones: custom avatars
    are a paid add-on, so most accounts run a stock one (the Sandbox's included).
    Each half degrades alone: one request failing keeps what the other found,
    and only both failing is the catalog's failure (stale list or empty)."""
    own = await _attempt(_liveavatar_page(client, key, _LIVEAVATAR_AVATARS))
    stock = await _attempt(_liveavatar_page(client, key, f"{_LIVEAVATAR_AVATARS}/public"))
    if isinstance(own, Exception) and isinstance(stock, Exception):
        raise own
    for half, result in (("own", own), ("stock", stock)):
        if isinstance(result, Exception):
            logger.warning("LiveAvatar %s avatars unavailable, listing the rest: %s", half, result)
    return [*_or_none(own), *_or_none(stock)]


async def _attempt(coro) -> list[dict[str, str]] | Exception:
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 — the caller decides what one failure costs
        return exc


def _or_none(result: list[dict[str, str]] | Exception) -> list[dict[str, str]]:
    return [] if isinstance(result, Exception) else result


async def _liveavatar_page(client: httpx.AsyncClient, key: str, url: str) -> list[dict[str, str]]:
    # One page at LiveAvatar's maximum; the field still takes a typed ID past it.
    r = await client.get(url, params={"page_size": 100}, headers=_liveavatar_headers(key))
    r.raise_for_status()
    results = (r.json().get("data") or {}).get("results", [])
    pairs = [_pair(a["id"], a.get("name") or a["id"]) for a in results if _live(a)]
    return sorted(pairs, key=lambda p: p["label"])


async def _tavus_faces(client: httpx.AsyncClient, key: str) -> list[dict[str, str]]:
    """The account's Avatars. Tavus calls them faces (`/v2/faces`, `face_id`);
    `/v2/replicas` and `replica_id` stay as its aliases, so either spelling of a
    row is read, and the ID is what TurnCall's `replica_id` takes."""
    r = await client.get("https://tavusapi.com/v2/faces", params={"limit": 100}, headers={"x-api-key": key})
    r.raise_for_status()
    pairs = []
    for face in r.json().get("data", []):
        if not isinstance(face, dict) or face.get("status", "completed") != "completed":
            continue  # `started` is still training, `error` never will
        face_id = face.get("face_id") or face.get("replica_id")
        if face_id:
            pairs.append(_pair(face_id, face.get("face_name") or face.get("replica_name") or face_id))
    return sorted(pairs, key=lambda p: p["label"])


def _live(avatar: Any) -> bool:
    """An avatar a session can start with: not training, failed or expired.
    A row that states no status is offered rather than guessed about."""
    return (
        isinstance(avatar, dict)
        and bool(avatar.get("id"))
        and avatar.get("status", "ACTIVE") == "ACTIVE"
        and not avatar.get("is_expired")
    )


# --- credential validation (#56) ---------------------------------------------

# The list endpoint each account's key is checked against — the same calls the
# pickers make, except OpenRouter's, whose model list is public and so would
# accept any key; its key endpoint is what actually authenticates.
_PROBES: dict[str, tuple[str, Any]] = {
    "openai": ("https://api.openai.com/v1/models", lambda k: {"Authorization": f"Bearer {k}"}),
    "anthropic": (
        "https://api.anthropic.com/v1/models",
        lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01"},
    ),
    "openrouter": ("https://openrouter.ai/api/v1/key", lambda k: {"Authorization": f"Bearer {k}"}),
    "google": ("https://generativelanguage.googleapis.com/v1beta/models", lambda k: {"x-goog-api-key": k}),
    "deepgram": ("https://api.deepgram.com/v1/projects", lambda k: {"Authorization": f"Token {k}"}),
    "elevenlabs": ("https://api.elevenlabs.io/v1/voices", lambda k: {"xi-api-key": k}),
    "cartesia": (
        "https://api.cartesia.ai/voices",
        lambda k: {"X-API-Key": k, "Cartesia-Version": "2024-06-10"},
    ),
    "tavus": ("https://tavusapi.com/v2/replicas", lambda k: {"x-api-key": k}),
    # A HeyGen key is a LiveAvatar key: pipecat's HeyGen service calls LiveAvatar.
    "heygen": (_LIVEAVATAR_AVATARS, _liveavatar_headers),
    # Telnyx: the balance needs the key and changes nothing (#84).
    "telnyx": ("https://api.telnyx.com/v2/balance", lambda k: {"Authorization": f"Bearer {k}"}),
    # AssemblyAI has no models endpoint; its transcript list authenticates.
    "assemblyai": ("https://api.assemblyai.com/v2/transcript?limit=1", lambda k: {"Authorization": k}),
    # xAI's own key endpoint: it authenticates whatever the key's ACLs allow.
    "xai": ("https://api.x.ai/v1/api-key", lambda k: {"Authorization": f"Bearer {k}"}),
}
# Providers that refuse a wrong key with a 400 rather than a 401/403, and the
# words their body says it with (xAI: "Incorrect API key provided", measured
# 2026-10-21). Any other 400 from them stays unverified.
_BAD_KEY_400 = {"xai": "Incorrect API key"}


async def probe(provider: str, document: dict[str, Any]) -> dict[str, Any]:
    """Check a credential against its provider's list endpoint.

    The result names what was checked and never claims the key "works": a key
    a list endpoint accepts can still lack the scopes a call needs.

    status: `valid` (the endpoint accepted it), `rejected` (401/403 — the caller
    must not store it as connected), `unverified` (the endpoint could not be
    reached or answered otherwise), `unchecked` (no list endpoint to ask).
    """
    target = _PROBES.get(provider)
    key = document.get("api_key")
    if target is None or not key:
        return {"status": "unchecked", "checked": None,
                "detail": f"{provider} has no list endpoint the Builder can check a key against"}
    url, headers = target
    checked = f"GET {url}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=headers(key))
    except httpx.HTTPError as exc:
        return {"status": "unverified", "checked": checked, "detail": f"unreachable: {type(exc).__name__}"}
    if r.status_code in (401, 403):
        return {"status": "rejected", "checked": checked, "detail": f"{provider} answered {r.status_code}"}
    words = _BAD_KEY_400.get(provider)
    if r.status_code == 400 and words and words in r.text:
        return {"status": "rejected", "checked": checked, "detail": f"{provider} answered 400: the key is incorrect"}
    if r.is_success:
        return {"status": "valid", "checked": checked, "detail": f"accepted by {provider}'s list endpoint"}
    return {"status": "unverified", "checked": checked, "detail": f"{provider} answered {r.status_code}"}
