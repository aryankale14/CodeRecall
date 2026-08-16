"""
Diagnostic for the Gemini key pool.

Run:  venv/Scripts/python.exe test_fallback.py

Checks that every configured key is live, that keys rotate when one is out of
quota, and - most importantly - which Google Cloud project each key belongs to.
Free-tier quota is metered PER PROJECT, so keys created in the same project
share a single daily allowance and give you no extra headroom.
"""

import asyncio
import os
import re
import sys
import urllib.error
import urllib.request
import warnings

warnings.filterwarnings("ignore")
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.config import get_settings                                    # noqa: E402
from app.services.gemini_pool import AllKeysExhausted, get_pool        # noqa: E402

PROJECT_RE = re.compile(r"project[s]?[ /](\d{6,})")


def resolve_project(api_key: str) -> str:
    """
    Ask a Google API that is not enabled on the project. The 403 names the
    project number, which identifies the quota pool. Costs no Gemini quota.
    """
    try:
        urllib.request.urlopen(
            "https://texttospeech.googleapis.com/v1/voices?key=" + api_key, timeout=20
        )
        return "unknown"
    except urllib.error.HTTPError as e:
        match = PROJECT_RE.search(e.read().decode())
        return match.group(1) if match else "unknown"
    except Exception:
        return "unknown"


async def main():
    settings = get_settings()
    pool = get_pool()

    print("=" * 72)
    print("1. KEYS AND THEIR QUOTA POOLS")
    print("=" * 72)
    named = [
        ("MAP", settings.GEMINI_API_KEY_MAP),
        ("REDUCE", settings.GEMINI_API_KEY_REDUCE),
        ("RAG", settings.GEMINI_API_KEY_RAG),
        ("MAP_FALLBACK", settings.GEMINI_API_KEY_MAP_FALLBACK),
        ("REDUCE_FALLBACK", settings.GEMINI_API_KEY_REDUCE_FALLBACK),
        ("RAG_FALLBACK", settings.GEMINI_API_KEY_RAG_FALLBACK),
    ]
    pools: dict[str, list[str]] = {}
    for label, key in named:
        if not key:
            print(f"  {label:18} MISSING")
            continue
        project = await asyncio.to_thread(resolve_project, key)
        pools.setdefault(project, []).append(label)
        print(f"  {label:18} {key[:8]}..{key[-4:]}   project={project}")

    print()
    shared = {p: names for p, names in pools.items() if len(names) > 1 and p != "unknown"}
    identified = [p for p in pools if p != "unknown"]
    if shared:
        print("  WARNING - these keys share one daily quota pool:")
        for project, names in shared.items():
            print(f"    project {project}: {', '.join(names)}")
        print("  Rotating between them buys nothing. Create each key in a")
        print("  SEPARATE Google Cloud project to get separate allowances.")
    elif not identified:
        print("  Could not determine any key's project. AI Studio 'express' keys")
        print("  (the AQ.* format) reject the probe used here, so separation")
        print("  CANNOT be verified from the key alone - confirm in AI Studio that")
        print("  each key was created under a different project.")
    else:
        print(f"  {len(identified)} distinct project(s) identified; none shared.")

    print()
    print("=" * 72)
    print("2. LIVE CALL THROUGH THE POOL")
    print("=" * 72)
    try:
        answer = await pool.generate(settings.GEMINI_MODEL_MAP, "Reply with exactly: OK")
        print(f"  generate({settings.GEMINI_MODEL_MAP}) -> {answer.strip()[:40]!r}")
    except AllKeysExhausted as e:
        print(f"  EXHAUSTED: {e}")

    print()
    print("=" * 72)
    print("3. BATCHED EMBEDDING (must be 1 request for many chunks)")
    print("=" * 72)
    try:
        vectors = await pool.embed([f"chunk number {i}" for i in range(5)])
        print(f"  5 chunks -> {len(vectors)} vectors of dim {len(vectors[0])} in one request")
    except AllKeysExhausted as e:
        print(f"  EXHAUSTED: {e}")

    print()
    print("=" * 72)
    print("4. ROTATION (bench every key but the last)")
    print("=" * 72)
    import time
    model = settings.GEMINI_MODEL_MAP
    for state in pool._states[:-1]:
        state.dead_until[model] = time.time() + 3600
    try:
        answer = await pool.generate(model, "Reply with exactly: ROTATED")
        print(f"  survived on {pool._states[-1].label} -> {answer.strip()[:40]!r}")
    except AllKeysExhausted as e:
        print(f"  EXHAUSTED: {e}")


if __name__ == "__main__":
    asyncio.run(main())
