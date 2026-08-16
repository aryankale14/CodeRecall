"""
Quota-aware Gemini key pool.

This replaces the previous per-role fallback logic, which could not actually
recover from an exhausted key:

  * The embedding fallback assigned to ``embeddings_model.google_api_key``.
    That field is read once when the underlying client is built, so the retry
    went out on the *same* exhausted key.
  * Every 429 was treated as transient, so a per-day exhaustion burned five
    tenacity retries per file (each of which internally tried two models)
    instead of rotating to a key that still had quota.
  * Key swaps went through the process-global ``genai.configure()``. The RAG
    chat endpoint and the background map phase both call it, so a chat during
    an ingest could silently repoint the worker at the wrong key.

Here every key owns an isolated client, so there is no global state to clobber,
and 429s are classified before we react to them: a per-minute limit means wait,
a per-day limit means this key is finished for that model until quota resets.
"""

import asyncio
import re
import time
from dataclasses import dataclass, field

from google import genai
from google.genai import types

from app.config import get_settings

# How long a key stays benched after it reports a per-day exhaustion. The real
# free-tier window resets at midnight Pacific; an hour keeps a long-lived server
# self-healing without re-probing a dead key on every file.
RPD_COOLDOWN_SECONDS = 3600.0

# Ceiling on how long we honour a server-supplied RetryInfo before rotating.
MAX_RETRY_DELAY_SECONDS = 30.0

# Tried in order once the requested model is exhausted on every key. Ordered by
# descending free-tier requests-per-day, so we degrade toward more headroom
# rather than toward a bigger model.
#
# 3.x only: the 2.x series is not served to newly created API keys, so listing
# a 2.x model here would just add a guaranteed-failing round trip per file.
DOWNGRADE_LADDER = (
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
)

EMBED_MODEL = "gemini-embedding-2"
EMBED_DIMENSIONS = 3072  # must match the Vector(3072) column in models.py

# Google caps batchEmbedContents at 100 inputs per request.
MAX_EMBED_BATCH = 100


class AllKeysExhausted(RuntimeError):
    """Every configured key is out of quota for every candidate model."""


@dataclass
class _KeyState:
    label: str
    key: str
    client: genai.Client
    # model name -> unix timestamp until which this key is out of daily quota
    dead_until: dict[str, float] = field(default_factory=dict)
    # earliest time this key may be used again (per-minute pacing / backoff)
    next_free_at: float = 0.0

    def is_dead_for(self, model: str, now: float) -> bool:
        return self.dead_until.get(model, 0.0) > now


def _classify_quota_error(exc: Exception) -> tuple[str | None, float]:
    """
    Map an exception onto ("day" | "minute" | None, retry_after_seconds).

    Returns ``(None, 0)`` for anything that is not a quota rejection, so real
    bugs (bad model name, malformed prompt, auth failure) surface immediately
    instead of being retried into the ground.
    """
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    text = str(exc)

    is_429 = code == 429 or "RESOURCE_EXHAUSTED" in text.upper() or "429" in text
    if not is_429:
        return None, 0.0

    # Google names the violated quota, e.g.
    #   GenerateRequestsPerDayPerProjectPerModel-FreeTier
    #   GenerateRequestsPerMinutePerProjectPerModel-FreeTier
    lowered = text.lower()
    if "perday" in lowered.replace(" ", "") or "per day" in lowered:
        scope = "day"
    elif "perminute" in lowered.replace(" ", "") or "per minute" in lowered:
        scope = "minute"
    else:
        # Unlabelled 429. Treat as a minute limit: the worst case is one wasted
        # wait, whereas guessing "day" would bench a key that is still usable.
        scope = "minute"

    delay = 0.0
    match = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", text)
    if match:
        delay = min(float(match.group(1)), MAX_RETRY_DELAY_SECONDS)
    return scope, delay


class GeminiPool:
    """Rotates a set of API keys across a ladder of models."""

    def __init__(self, min_interval: float = 4.0):
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._states: list[_KeyState] = []
        self._cursor = 0
        self._build()

    def _build(self) -> None:
        settings = get_settings()
        candidates = [
            ("MAP", settings.GEMINI_API_KEY_MAP),
            ("REDUCE", settings.GEMINI_API_KEY_REDUCE),
            ("RAG", settings.GEMINI_API_KEY_RAG),
            ("MAP_FALLBACK", settings.GEMINI_API_KEY_MAP_FALLBACK),
            ("REDUCE_FALLBACK", settings.GEMINI_API_KEY_REDUCE_FALLBACK),
            ("RAG_FALLBACK", settings.GEMINI_API_KEY_RAG_FALLBACK),
        ]
        seen: set[str] = set()
        for label, key in candidates:
            if not key or key in seen:
                continue
            seen.add(key)
            self._states.append(
                _KeyState(label=label, key=key, client=genai.Client(api_key=key))
            )
        print(f"[POOL] Initialised with {len(self._states)} distinct Gemini key(s): "
              f"{', '.join(s.label for s in self._states)}")

    @property
    def size(self) -> int:
        return len(self._states)

    def _model_chain(self, model: str) -> list[str]:
        chain = [model]
        chain.extend(m for m in DOWNGRADE_LADDER if m != model)
        return chain

    async def _acquire(self, model: str) -> _KeyState | None:
        """
        Hand back the next key usable for ``model``, sleeping if they are all
        merely pacing. Returns None when every key is out of daily quota.
        """
        async with self._lock:
            now = time.time()
            live = [s for s in self._states if not s.is_dead_for(model, now)]
            if not live:
                return None

            # Prefer a key that is ready right now, round-robin from the cursor.
            for offset in range(len(self._states)):
                state = self._states[(self._cursor + offset) % len(self._states)]
                if state.is_dead_for(model, now) or state.next_free_at > now:
                    continue
                self._cursor = (self._cursor + offset + 1) % len(self._states)
                state.next_free_at = now + self._min_interval
                return state

            # Every live key is still cooling down. Reserve the one that frees
            # up soonest by pushing its slot forward now, so concurrent callers
            # each reserve a different slot instead of piling onto one key.
            state = min(live, key=lambda s: s.next_free_at)
            wait = max(0.0, state.next_free_at - now)
            state.next_free_at = now + wait + self._min_interval

        # Sleep outside the lock: holding it here would make a background map
        # phase block an interactive RAG chat for the whole pacing interval.
        if wait:
            await asyncio.sleep(wait)
        return state

    async def _run(self, chain: list[str], call, what: str):
        """
        Execute ``call(client, model_name)`` against the pool, rotating keys on
        quota errors and stepping down ``chain`` once every key is spent on the
        current model.
        """
        last_exc: Exception | None = None
        for index, candidate in enumerate(chain):
            # At most one full sweep of the pool per model.
            for _ in range(max(1, len(self._states))):
                state = await self._acquire(candidate)
                if state is None:
                    break
                try:
                    return await call(state.client, candidate)
                except Exception as exc:  # noqa: BLE001 - re-raised unless quota
                    scope, delay = _classify_quota_error(exc)
                    if scope is None:
                        raise
                    last_exc = exc
                    if scope == "day":
                        state.dead_until[candidate] = time.time() + RPD_COOLDOWN_SECONDS
                        print(f"[POOL] {what}: key {state.label} is out of DAILY quota "
                              f"for {candidate}; benching it and rotating.")
                    else:
                        state.next_free_at = time.time() + max(delay, self._min_interval)
                        print(f"[POOL] {what}: key {state.label} hit a per-minute limit "
                              f"on {candidate}; rotating (retry in "
                              f"{delay or self._min_interval:.0f}s).")
            if index < len(chain) - 1:
                print(f"[POOL] {what}: every key is spent on {candidate}; "
                      f"falling back to {chain[index + 1]}.")

        raise AllKeysExhausted(
            f"{what}: all {len(self._states)} key(s) are out of quota across "
            f"{', '.join(chain)}. Free-tier quota is metered per Google Cloud "
            f"project - keys created in the same project share one pool."
        ) from last_exc

    async def generate(self, model: str, prompt: str,
                       response_mime_type: str | None = None) -> str:
        config = None
        if response_mime_type:
            config = types.GenerateContentConfig(response_mime_type=response_mime_type)

        async def call(client: genai.Client, model_name: str) -> str:
            response = await client.aio.models.generate_content(
                model=model_name, contents=prompt, config=config
            )
            return response.text or ""

        return await self._run(self._model_chain(model), call, "generate")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """
        Embed every string in ``texts`` using a single batched request per
        chunk-of-100, rather than one request per string.
        """
        if not texts:
            return []

        config = types.EmbedContentConfig(output_dimensionality=EMBED_DIMENSIONS)
        vectors: list[list[float]] = []

        for start in range(0, len(texts), MAX_EMBED_BATCH):
            # Each string must be its own Content. Passing a bare list[str] makes
            # the SDK fold them into a single multi-part Content, which returns
            # one merged vector instead of one vector per chunk.
            batch = [types.Content(parts=[types.Part(text=t)])
                     for t in texts[start:start + MAX_EMBED_BATCH]]

            async def call(client: genai.Client, model_name: str, _batch=batch):
                response = await client.aio.models.embed_content(
                    model=model_name, contents=_batch, config=config
                )
                return [list(e.values) for e in response.embeddings]

            # Embedding models have their own quota bucket and no lite variant,
            # so the chain is just the one model - only keys rotate.
            vectors.extend(await self._run([EMBED_MODEL], call, "embed"))

        return vectors

    async def embed_one(self, text: str) -> list[float]:
        result = await self.embed([text])
        return result[0]


_pool: GeminiPool | None = None


def get_pool() -> GeminiPool:
    """Process-wide singleton, built lazily so settings are read once."""
    global _pool
    if _pool is None:
        _pool = GeminiPool()
    return _pool
