"""Feature 4: Pull request review assistant."""

import logging

from app.agent.core import run_agent
from app.agent.prompts import PR_REVIEW_PROMPT
from app.github.client import GitHubClient

logger = logging.getLogger(__name__)


async def run(data: dict) -> str:
    """
    Review a pull request and return a structured markdown summary.

    The agent reads each changed file before producing specific,
    actionable feedback grounded in real diffs.
    """
    repo_full_name: str = data["repository"]["full_name"]
    installation_id: int = data["installation"]["id"]
    pr = data["pull_request"]

    client = GitHubClient(installation_id)
    changed_files = client.get_pull_request_files(repo_full_name, pr["number"])

    files_summary = "\n".join(
        f"- {f['filename']} ({f['status']})" for f in changed_files
    ) or "No files listed."

    _MAX_PATCH_CHARS = 6_000
    raw_patches = "\n\n".join(
        f"### {f['filename']}\n```diff\n{f['patch']}\n```"
        for f in changed_files
        if f.get("patch")
    )
    patch_text = (
        raw_patches[:_MAX_PATCH_CHARS] + "\n... [diff truncated — open the PR to see remaining changes]"
        if len(raw_patches) > _MAX_PATCH_CHARS
        else raw_patches
    )

    user_message = (
        f"Repository: {repo_full_name}\n"
        f"PR #{pr['number']}: {pr['title']}\n\n"
        f"PR description:\n{pr.get('body') or '(no description provided)'}\n\n"
        f"Files changed:\n{files_summary}\n\n"
        f"Diffs:\n{patch_text}\n\n"
        "Please review this pull request."
    )

    context = {
        "github_client": client,
        "repo_full_name": repo_full_name,
        "feature": "pr_review",
    }

    result = await run_agent(PR_REVIEW_PROMPT, user_message, context)
    logger.info(
        "PR review complete for PR #%d", pr["number"],
        extra={"feature": "pr_review", "repo": repo_full_name},
    )
    return result
