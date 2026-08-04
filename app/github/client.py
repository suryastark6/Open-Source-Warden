"""GitHub API wrapper providing read operations used by the agent tools."""

import itertools
import logging
import time
from typing import Any

from github import Github, GithubException
from github.ContentFile import ContentFile

from app.github.auth import get_github_client

logger = logging.getLogger(__name__)

# Module-level TTL cache shared across all GitHubClient instances.
# When a new issue triggers triage → reproduction → onboarding in sequence,
# the README and file contents are fetched only once from GitHub.
_CACHE: dict[str, tuple[Any, float]] = {}
_CACHE_TTL = 300  # seconds


def _cache_get(key: str) -> Any:
    entry = _CACHE.get(key)
    if entry is not None and entry[1] > time.monotonic():
        return entry[0]
    return None


def _cache_set(key: str, value: Any) -> None:
    _CACHE[key] = (value, time.monotonic() + _CACHE_TTL)


class GitHubClient:
    """Thin wrapper around PyGithub for the operations Open-Source-Warden needs."""

    def __init__(self, installation_id: int) -> None:
        self._gh: Github = get_github_client(installation_id)

    def read_file(self, repo_full_name: str, path: str) -> str:
        """Return the decoded contents of a file, or an error message."""
        key = f"file:{repo_full_name}:{path}"
        cached = _cache_get(key)
        if cached is not None:
            return cached
        try:
            repo = self._gh.get_repo(repo_full_name)
            content: ContentFile = repo.get_contents(path)  # type: ignore[assignment]
            if isinstance(content, list):
                return f"Path '{path}' is a directory, not a file."
            result = content.decoded_content.decode("utf-8", errors="replace")
            _cache_set(key, result)
            return result
        except GithubException as exc:
            logger.warning("read_file failed for %s/%s: %s", repo_full_name, path, exc)
            return f"Could not read file '{path}': {exc.data.get('message', str(exc))}"

    def list_files(self, repo_full_name: str, path: str) -> list[dict[str, str]]:
        """List directory contents at *path* (empty string = repo root)."""
        key = f"ls:{repo_full_name}:{path}"
        cached = _cache_get(key)
        if cached is not None:
            return cached
        try:
            repo = self._gh.get_repo(repo_full_name)
            contents = repo.get_contents(path or "")
            if not isinstance(contents, list):
                contents = [contents]
            result = [{"name": c.name, "path": c.path, "type": c.type} for c in contents]
            _cache_set(key, result)
            return result
        except GithubException as exc:
            logger.warning("list_files failed for %s/%s: %s", repo_full_name, path, exc)
            return []

    def search_code(self, repo_full_name: str, query: str) -> list[dict[str, str]]:
        """Search for *query* across the repository's code."""
        try:
            results = self._gh.search_code(f"{query} repo:{repo_full_name}")
            return [{"path": item.path, "name": item.name} for item in itertools.islice(results, 10)]
        except GithubException as exc:
            logger.warning("search_code failed for %s: %s", repo_full_name, exc)
            return []

    def get_recent_issues(self, repo_full_name: str, count: int = 10) -> list[dict[str, Any]]:
        """Return up to *count* recently closed issues."""
        key = f"issues:{repo_full_name}:{count}"
        cached = _cache_get(key)
        if cached is not None:
            return cached
        try:
            repo = self._gh.get_repo(repo_full_name)
            issues = repo.get_issues(state="closed", sort="updated")
            result = [
                {"number": i.number, "title": i.title, "labels": [lb.name for lb in i.labels]}
                for i in itertools.islice(issues, count)
            ]
            _cache_set(key, result)
            return result
        except GithubException as exc:
            logger.warning("get_recent_issues failed for %s: %s", repo_full_name, exc)
            return []

    def get_readme(self, repo_full_name: str) -> str:
        """Fetch the repository README."""
        key = f"readme:{repo_full_name}"
        cached = _cache_get(key)
        if cached is not None:
            return cached
        try:
            repo = self._gh.get_repo(repo_full_name)
            readme = repo.get_readme()
            result = readme.decoded_content.decode("utf-8", errors="replace")
            _cache_set(key, result)
            return result
        except GithubException as exc:
            logger.warning("get_readme failed for %s: %s", repo_full_name, exc)
            return "README not found."

    def get_repo_labels(self, repo_full_name: str) -> list[str]:
        """Return all label names defined on the repository."""
        key = f"labels:{repo_full_name}"
        cached = _cache_get(key)
        if cached is not None:
            return cached
        try:
            repo = self._gh.get_repo(repo_full_name)
            result = [lb.name for lb in repo.get_labels()]
            _cache_set(key, result)
            return result
        except GithubException as exc:
            logger.warning("get_repo_labels failed for %s: %s", repo_full_name, exc)
            return []

    def post_comment(self, repo_full_name: str, issue_number: int, body: str) -> None:
        """Post a comment on an issue or pull request."""
        if not body or not body.strip():
            logger.error(
                "post_comment skipped for %s#%d: body is blank", repo_full_name, issue_number
            )
            return
        try:
            repo = self._gh.get_repo(repo_full_name)
            issue = repo.get_issue(issue_number)
            issue.create_comment(body)
        except GithubException as exc:
            logger.error(
                "post_comment failed for %s#%d: %s", repo_full_name, issue_number, exc
            )

    def get_pull_request_files(self, repo_full_name: str, pr_number: int) -> list[dict[str, str]]:
        """Return files changed in a pull request."""
        try:
            repo = self._gh.get_repo(repo_full_name)
            pr = repo.get_pull(pr_number)
            return [{"filename": f.filename, "status": f.status, "patch": f.patch or ""} for f in pr.get_files()]
        except GithubException as exc:
            logger.warning("get_pull_request_files failed for %s#%d: %s", repo_full_name, pr_number, exc)
            return []

    def get_merged_prs(self, repo_full_name: str, since_tag: str | None = None) -> list[dict[str, Any]]:
        """Return recently merged pull requests."""
        try:
            repo = self._gh.get_repo(repo_full_name)
            pulls = repo.get_pulls(state="closed", sort="updated", base=repo.default_branch)
            merged = [p for p in itertools.islice(pulls, 50) if p.merged]
            return [
                {"number": p.number, "title": p.title, "body": p.body or "", "labels": [lb.name for lb in p.labels]}
                for p in merged
            ]
        except GithubException as exc:
            logger.warning("get_merged_prs failed for %s: %s", repo_full_name, exc)
            return []
