"""
Thin compatibility layer over the quota-aware key pool.

The old implementation here fell back from one model to another on the *same*
API key, and always fell back "upward" (gemini-2.5-flash -> gemini-3.5-flash),
which asks for a model with *less* free-tier headroom than the one that just
ran out. It also never rotated keys. Both jobs now belong to GeminiPool, which
rotates keys first and only then steps down to a higher-quota model.
"""

import asyncio
import time

from app.services.gemini_pool import AllKeysExhausted, get_pool  # noqa: F401

last_request_time = 0.0
request_lock = asyncio.Lock()


async def space_request(min_interval: float = 4.0):
    """
    Kept for call sites that still pace themselves explicitly.

    The pool now paces each key independently, so this is a no-op unless a
    caller asks for spacing wider than the pool's own interval.
    """
    global last_request_time
    async with request_lock:
        now = time.time()
        elapsed = now - last_request_time
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)
        last_request_time = time.time()


async def generate_content_with_fallback(model_name: str, prompt: str,
                                         response_mime_type: str = None) -> str:
    """Generate content, rotating across every configured key as needed."""
    return await get_pool().generate(
        model=model_name,
        prompt=prompt,
        response_mime_type=response_mime_type,
    )
