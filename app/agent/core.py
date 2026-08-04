"""Agentic loop powered by NVIDIA Nemotron-3-Super via NVIDIA NIM API."""

import asyncio
import json
import logging
import time
from typing import Any

from openai import AsyncOpenAI

from app.agent.safety import is_safe_action
from app.agent.tools import TOOL_DEFINITIONS, execute_tool
from app.config import settings

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(
    base_url=settings.NVIDIA_BASE_URL,
    api_key=settings.NVIDIA_API_KEY,
)


async def _run_tool_call(
    tool_call: Any, context: dict[str, Any], feature: str, repo: str
) -> tuple[str, str]:
    """Execute one tool call and return (tool_call_id, result).

    Errors are caught here so a single bad tool call never aborts the whole
    gather — the model just sees an error message for that slot.
    """
    tool_name = tool_call.function.name
    try:
        tool_args = json.loads(tool_call.function.arguments)
    except json.JSONDecodeError:
        tool_args = {}

    if not is_safe_action(tool_name, tool_args):
        logger.warning(
            "Blocked unsafe tool call: %s", tool_name,
            extra={"feature": feature, "repo": repo},
        )
        return tool_call.id, "Action blocked by safety guardrails. Read-only operations only."

    try:
        result = await execute_tool(tool_name, tool_args, context)
        return tool_call.id, result
    except Exception as exc:
        logger.warning(
            "Tool %s raised unexpected error: %s", tool_name, exc,
            extra={"feature": feature, "repo": repo},
        )
        return tool_call.id, f"Tool '{tool_name}' encountered an error: {exc}"


async def run_agent(
    system_prompt: str,
    user_message: str,
    context: dict[str, Any],
    max_iterations: int = 5,
) -> str:
    """
    Agentic loop: Nemotron-3-Super reasons, calls tools, reasons again,
    until it has enough information to produce a final response.

    Tool calls within a single LLM round are executed concurrently via
    asyncio.gather, which combined with asyncio.to_thread in execute_tool
    gives true parallelism for the underlying synchronous GitHub API calls.

    Returns the model's final text response.
    """
    repo = context.get("repo_full_name", "unknown")
    feature = context.get("feature", "core")
    start = time.monotonic()
    tool_call_count = 0

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    for iteration in range(max_iterations):
        response = await _client.chat.completions.create(
            model=settings.NVIDIA_MODEL,
            messages=messages,
            tools=TOOL_DEFINITIONS,
            tool_choice="auto",
            temperature=0.2,
            max_tokens=2048,
        )

        if not response.choices:
            logger.error("NIM API returned empty choices", extra={"feature": feature, "repo": repo})
            return (
                "I was unable to complete the analysis (empty model response).\n\n"
                "---\n*Powered by NVIDIA Nemotron-3-Super via Open-Source-Warden*"
            )

        choice = response.choices[0]
        message = choice.message

        if choice.finish_reason == "length" and not message.tool_calls:
            elapsed = time.monotonic() - start
            logger.warning(
                "Model hit max_tokens limit after %.2fs — input diff likely too large",
                elapsed,
                extra={"feature": feature, "repo": repo},
            )
            return (
                "The analysis could not be completed — the pull request diff is too large for a single pass. "
                "Consider splitting the PR into smaller changes.\n\n"
                "---\n*Powered by NVIDIA Nemotron-3-Super via Open-Source-Warden*"
            )

        if not message.tool_calls:
            elapsed = time.monotonic() - start
            content = message.content or ""
            if not content.strip():
                logger.warning(
                    "Model returned empty content after %.2fs",
                    elapsed,
                    extra={"feature": feature, "repo": repo},
                )
                return (
                    "The analysis could not be completed (the model returned an empty response). "
                    "Please try again or trigger the command manually.\n\n"
                    "---\n*Powered by NVIDIA Nemotron-3-Super via Open-Source-Warden*"
                )
            logger.info(
                "Agent finished in %.2fs after %d tool calls",
                elapsed,
                tool_call_count,
                extra={"feature": feature, "repo": repo},
            )
            return content

        messages.append(message)

        # Execute all tool calls in this round concurrently.
        # asyncio.to_thread inside execute_tool ensures the synchronous
        # PyGithub HTTP calls don't block each other.
        tasks = [
            _run_tool_call(tc, context, feature, repo)
            for tc in message.tool_calls
        ]
        results = await asyncio.gather(*tasks)
        tool_call_count += len(results)

        for tool_call_id, tool_result in results:
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": str(tool_result),
            })

    return (
        "I was unable to complete the analysis within the allowed steps. "
        "Please try again.\n\n"
        "---\n*Powered by NVIDIA Nemotron-3-Super via Open-Source-Warden*"
    )
