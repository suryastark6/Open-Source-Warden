"""GitHub webhook receiver, signature verifier, and event router."""

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.agent.prompts import HELP_MESSAGE
from app.config import settings
from app.features import onboarding, pr_review, release_notes, reproduction, triage
from app.github.client import GitHubClient
from app.security import verify_signature

router = APIRouter()
logger = logging.getLogger(__name__)

COPILOT_COMMANDS = {"/copilot triage", "/copilot repro", "/copilot onboard",
                   "/copilot review", "/copilot release-notes", "/copilot help"}


@router.post("/webhook")
async def github_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    """Receive a GitHub webhook, verify its signature, and dispatch handlers."""
    payload = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    event = request.headers.get("X-GitHub-Event", "")

    if not verify_signature(payload, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    data = await request.json()

    if event == "issues" and data.get("action") == "opened":
        background_tasks.add_task(_handle_new_issue, data)

    elif event == "pull_request" and data.get("action") in ("opened", "synchronize"):
        background_tasks.add_task(_handle_new_pr, data)

    elif event == "issue_comment" and data.get("action") == "created":
        background_tasks.add_task(_handle_comment_command, data)

    elif event == "push":
        background_tasks.add_task(_handle_push, data)

    return {"status": "accepted"}


async def _post_comment(
    repo_full_name: str, issue_number: int, installation_id: int, body: str
) -> None:
    client = GitHubClient(installation_id)
    client.post_comment(repo_full_name, issue_number, body)


async def _handle_new_issue(data: dict) -> None:
    """Run triage (and optionally reproduction + onboarding) on a new issue."""
    repo_full_name = data["repository"]["full_name"]
    issue_number = data["issue"]["number"]
    installation_id = data["installation"]["id"]
    extra = {"feature": "triage", "repo": repo_full_name}

    try:
        if settings.FEATURE_TRIAGE:
            triage_result = await triage.run(data)
            await _post_comment(repo_full_name, issue_number, installation_id, triage_result)

            # Determine which follow-up features are needed, then run them in parallel.
            run_repro = settings.FEATURE_REPRODUCTION and "bug" in triage_result.lower()
            run_onboard = settings.FEATURE_ONBOARDING and triage.is_good_first_issue(triage_result)

            follow_up_tasks = []
            if run_repro:
                follow_up_tasks.append(reproduction.run(data))
            if run_onboard:
                follow_up_tasks.append(onboarding.run(data))

            if follow_up_tasks:
                follow_up_results = await asyncio.gather(*follow_up_tasks)
                result_idx = 0
                if run_repro:
                    await _post_comment(
                        repo_full_name, issue_number, installation_id, follow_up_results[result_idx]
                    )
                    result_idx += 1
                if run_onboard:
                    await _post_comment(
                        repo_full_name, issue_number, installation_id, follow_up_results[result_idx]
                    )

    except Exception as exc:
        logger.error("Issue handling failed for %s#%d: %s", repo_full_name, issue_number, exc, extra=extra)
        await _post_comment(
            repo_full_name,
            issue_number,
            installation_id,
            "⚠️ Open-Source-Warden encountered an error during triage. "
            "A maintainer will review this issue manually.\n\n"
            "---\n*Powered by NVIDIA Nemotron-3-Super via Open-Source-Warden*",
        )


async def _handle_new_pr(data: dict) -> None:
    """Run PR review on a new or updated pull request."""
    repo_full_name = data["repository"]["full_name"]
    pr_number = data["pull_request"]["number"]
    installation_id = data["installation"]["id"]
    extra = {"feature": "pr_review", "repo": repo_full_name}

    try:
        if settings.FEATURE_PR_REVIEW:
            result = await pr_review.run(data)
            await _post_comment(repo_full_name, pr_number, installation_id, result)
    except Exception as exc:
        logger.error("PR review failed for %s#%d: %s", repo_full_name, pr_number, exc, extra=extra)
        await _post_comment(
            repo_full_name,
            pr_number,
            installation_id,
            "⚠️ Open-Source-Warden encountered an error during PR review. "
            "A maintainer will review manually.\n\n"
            "---\n*Powered by NVIDIA Nemotron-3-Super via Open-Source-Warden*",
        )


async def _handle_comment_command(data: dict) -> None:
    """Parse and dispatch /copilot slash commands from issue comments."""
    repo_full_name = data["repository"]["full_name"]
    issue_number = data["issue"]["number"]
    installation_id = data["installation"]["id"]
    comment_body: str = data["comment"]["body"].strip()
    extra = {"feature": "command", "repo": repo_full_name}

    if not comment_body.startswith("/copilot"):
        return

    command = comment_body.split("\n")[0].strip().lower()
    logger.info("Command received: %s on %s#%d", command, repo_full_name, issue_number, extra=extra)

    try:
        match command:
            case "/copilot triage":
                if settings.FEATURE_TRIAGE:
                    result = await triage.run(data)
                    await _post_comment(repo_full_name, issue_number, installation_id, result)

            case "/copilot repro":
                if settings.FEATURE_REPRODUCTION:
                    result = await reproduction.run(data)
                    await _post_comment(repo_full_name, issue_number, installation_id, result)

            case "/copilot onboard":
                if settings.FEATURE_ONBOARDING:
                    result = await onboarding.run(data)
                    await _post_comment(repo_full_name, issue_number, installation_id, result)

            case "/copilot review":
                if settings.FEATURE_PR_REVIEW and "pull_request" in data.get("issue", {}).get("html_url", ""):
                    result = await pr_review.run(data)
                    await _post_comment(repo_full_name, issue_number, installation_id, result)

            case "/copilot release-notes":
                if settings.FEATURE_RELEASE_NOTES:
                    result = await release_notes.run(data)
                    await _post_comment(repo_full_name, issue_number, installation_id, result)

            case "/copilot help":
                await _post_comment(repo_full_name, issue_number, installation_id, HELP_MESSAGE)

            case _:
                await _post_comment(
                    repo_full_name, issue_number, installation_id,
                    f"Unknown command `{command}`. Type `/copilot help` for a list of commands.\n\n"
                    "---\n*Powered by NVIDIA Nemotron-3-Super via Open-Source-Warden*",
                )

    except Exception as exc:
        logger.error("Command %s failed on %s#%d: %s", command, repo_full_name, issue_number, exc, extra=extra)
        await _post_comment(
            repo_full_name,
            issue_number,
            installation_id,
            "⚠️ Open-Source-Warden encountered an error processing your command.\n\n"
            "---\n*Powered by NVIDIA Nemotron-3-Super via Open-Source-Warden*",
        )


async def _handle_push(data: dict) -> None:
    """Handle push events — trigger release notes on tagged pushes."""
    repo_full_name = data["repository"]["full_name"]
    ref: str = data.get("ref", "")

    if not ref.startswith("refs/tags/"):
        return

    tag = ref.removeprefix("refs/tags/")

    # Find a pinned issue or fall back to logging only
    try:
        if settings.FEATURE_RELEASE_NOTES:
            result = await release_notes.run(data, since_tag=tag)
            logger.info(
                "Release notes drafted for tag %s on %s:\n%s", tag, repo_full_name, result,
                extra={"feature": "release_notes", "repo": repo_full_name},
            )
    except Exception as exc:
        logger.error(
            "Release notes failed for %s tag %s: %s", repo_full_name, tag, exc,
            extra={"feature": "release_notes", "repo": repo_full_name},
        )
