"""Makes a one-token call to each model live mode is configured for, to prove the wiring end to end.

    LLM_BACKEND=vertex VERTEX_PROJECT_ID=my-project uv run python tools/live_check.py

Prints OK and the model the API answered as, or the error class and a short message. Exits 1 when any call fails.
"""

import asyncio
import sys

import anthropic

from app.llm.client import LLM, LiveConfigError, Unavailable, from_env
from app.llm.request import Message, Request

MESSAGE_CHARS = 200


def _message(error: BaseException) -> str:
    # Vertex nests Google's own message, which names the model it resolved, inside the body.
    body = error.body if isinstance(error, anthropic.APIStatusError) else None
    inner = body.get("error") if isinstance(body, dict) else None
    if isinstance(inner, dict) and isinstance(inner.get("message"), str):
        return str(inner["message"])
    return error.message if isinstance(error, anthropic.APIError) else str(error)


def _short(exc: BaseException) -> str:
    cause = exc.__cause__ or exc
    status = f" ({cause.status_code})" if isinstance(cause, anthropic.APIStatusError) else ""
    text = " ".join(_message(cause).split())
    return f"{type(cause).__name__}{status}: {text[:MESSAGE_CHARS]}{'...' if len(text) > MESSAGE_CHARS else ''}"


async def check(llm: LLM, model: str) -> tuple[bool, str]:
    request = Request("live_check.v1", model, ("Reply with OK.",), (Message("user", ("ping",)),), max_tokens=1)
    try:
        # complete, not ask: a one-token reply always stops at max_tokens, which ask would treat as a failure.
        response = await llm.complete(request)
    except Unavailable as exc:
        return False, f"{model}: {_short(exc)}"
    return True, f"{model}: OK, answered as {response.model}"


async def main() -> int:
    try:
        llm = from_env()
    except LiveConfigError as exc:
        print(exc)
        return 2
    if llm is None:
        print("LLM_BACKEND is off, so there is nothing to check.")
        return 0
    print(f"backend {llm.provider}")
    results = [await check(llm, model) for model in dict.fromkeys((llm.fast_model, llm.main_model))]
    for _, line in results:
        print(line)
    return 0 if all(ok for ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
