# Open-Source-Warden

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![Docker](https://img.shields.io/badge/Docker-ready-blue)
![Powered by Nemotron](https://img.shields.io/badge/Powered%20by-NVIDIA%20Nemotron--3--Super-76b900)
![License: MIT](https://img.shields.io/badge/License-MIT-green)

> An AI GitHub App for open-source projects. Triages issues, reviews pull requests, guides new contributors, and drafts release notes — powered by NVIDIA Nemotron-3-Super (49B).

---

## Install in One Click

[![Install Open-Source-Warden](https://img.shields.io/badge/%E2%9C%85%20Install%20Open--Source--Warden-GitHub%20App-2ea44f?style=for-the-badge&logo=github)](https://github.com/apps/open-source-warden)

Pick your repository, click Install, and the bot starts working immediately — no config, no server to run. Once it's in:

- Open an issue and it posts a triage report within seconds
- Open a PR and it reviews every changed file
- Comment `/copilot help` to see what else it can do

![Watch installation walkthrough](https://github.com/user-attachments/assets/3363f22b-7f2f-490d-bf43-08e72ff2740f)

> Self-hosting instructions are below if you want to run your own instance.

---

## Why this exists

Most open-source projects are maintained by two or three people who handle everything — triaging issues, reviewing PRs, answering the same "how do I get started?" question from every new contributor. It compounds fast, and burnout follows.

Open-Source-Warden plugs into your repo as a GitHub App and handles the first-response layer. It reads your actual codebase before it replies — so the triage report references real files, the reproduction steps follow real code paths, and the onboarding guide points to the right directory. It doesn't guess.

---

## Live Demo — Powered by Railway

See the agent running live: a new GitHub issue triggers the full triage pipeline on a real repository deployed on Railway.

![Watch backend workings](https://github.com/user-attachments/assets/f1cdd610-9480-4340-ab45-2c3712076d60)


---


## What it does

### Issue Triage

Every new issue gets a triage report automatically: severity, category, suggested labels, relevant source files, and recommended next steps. Maintainers can skip the read-and-classify step entirely. 
click the image to play the video

[![Watch Triage and Reproduction Steps demo](https://img.youtube.com/vi/MDgrL2pT_kU/hqdefault.jpg)](https://youtu.be/MDgrL2pT_kU)

---

### Newcomer Onboarding

When an issue is tagged `good-first-issue`, the bot posts a contributor guide: where the code lives, how to run it locally, and what a clean fix would look like. New contributors get unblocked without pinging a maintainer.
click the image to play the video

[![Watch Newcomer Onboarding demo](https://img.youtube.com/vi/-1ckMerA4_0/hqdefault.jpg)](https://youtu.be/-1ckMerA4_0)

---

### PR Review

Every opened or updated pull request gets a structured review: what changed, potential concerns, things done well, and a merge checklist. The agent reads the actual diff and surrounding context before commenting.
click the image to play the video

[![Watch PR Review demo](https://img.youtube.com/vi/JdBVGGP7LMs/hqdefault.jpg)](https://youtu.be/JdBVGGP7LMs)

---

### Release Notes

Triggered by `/copilot release-notes` or a version tag push. Reads merged PRs since the last release and drafts categorised release notes ready to publish.
click the image to play the video

[![Watch Release Notes demo](https://img.youtube.com/vi/4YzMjovSab0/hqdefault.jpg)](https://youtu.be/4YzMjovSab0)

---

### Reproduction Steps

For bug reports, the agent traces the relevant code path and writes step-by-step reproduction instructions grounded in what the code actually does — not boilerplate advice.
click the image to play the video

[![Watch Triage and Reproduction Steps demo](https://img.youtube.com/vi/MDgrL2pT_kU/hqdefault.jpg)](https://youtu.be/MDgrL2pT_kU)

## How it works

---

```mermaid
flowchart TD
    A[GitHub Event\nissue opened / PR opened] --> B[FastAPI Webhook\n/webhook POST]
    B --> C{Event Router}
    C -->|issue.opened| D[Triage + Onboarding]
    C -->|pull_request.opened| E[PR Review]
    C -->|comment command| F[Manual Trigger]
    D & E & F --> G[Agent Core\nAgentic Loop]
    G <-->|Tool Calls| H[GitHub API\nread files, search code]
    G -->|Inference| I[NVIDIA NIM API\nNemotron-3-Super]
    I --> G
    G --> J[Post Comment\nback to GitHub]
```

GitHub sends a webhook event → FastAPI routes it → the agent reads your codebase using GitHub API tools → calls Nemotron-3-Super via NVIDIA NIM → posts the result as a comment. The agentic loop runs up to 10 tool-call iterations before producing a final answer, which is what lets it chase down real file paths rather than guessing.

---

## Self-hosting

You'll need two things before running:

| What | Where to get it | Takes |
|---|---|---|
| NVIDIA API Key | [build.nvidia.com](https://build.nvidia.com) → any model → Get API Key | ~3 min |
| ngrok Auth Token | [ngrok.com](https://ngrok.com) → Dashboard → Your Authtoken | ~2 min |

Everything else is pre-configured. The setup wizard handles the rest.

### Option A — Python

```bash
git clone https://github.com/V-S-Pranay/Open-Source-Warden.git
cd Open-Source-Warden
python start.py
```

The wizard installs dependencies, walks you through the two credentials above, starts ngrok, updates the webhook URL, and launches the server. Takes about five minutes end to end.

### Option B — Docker

```bash
git clone https://github.com/V-S-Pranay/Open-Source-Warden.git
cd Open-Source-Warden
python docker_start.py
```

Same wizard, but builds a Docker image and runs both the app and ngrok as containers. Requires Docker Desktop to be installed and running.

To stop: `docker compose --profile dev down`

---

## Manual commands

Comment on any issue or PR to trigger a feature on demand:

| Command | What it does |
|---|---|
| `/copilot triage` | Re-runs triage on this issue |
| `/copilot repro` | Generates reproduction steps |
| `/copilot onboard` | Posts a newcomer contributor guide |
| `/copilot review` | Re-runs PR review |
| `/copilot release-notes` | Drafts release notes |
| `/copilot help` | Lists all commands |

---

## Why Nemotron-3-Super

The core of this project is an agentic loop that reads real files before it writes anything. That requires a model that can plan a multi-step investigation, call tools reliably across several iterations, and synthesise information from many file reads into something coherent.

Nemotron-3-Super (49B) was chosen because it handles all three well. Smaller models tend to hallucinate file paths or give up mid-loop. Nemotron follows the code.

---

## Configuration

All settings live in `.env`. Copy `.env.example` to get started. The key ones:

```env
NVIDIA_API_KEY=               # your NIM key
GITHUB_APP_ID=                # filled automatically by the wizard
GITHUB_WEBHOOK_SECRET=        # filled automatically by the wizard
GITHUB_PRIVATE_KEY_PATH=./github_private_key.pem
```

Features can be toggled individually:

```env
FEATURE_TRIAGE=true
FEATURE_REPRODUCTION=true
FEATURE_ONBOARDING=true
FEATURE_PR_REVIEW=true
FEATURE_RELEASE_NOTES=true
```

---

## Development

```bash
pip install -r requirements-dev.txt

pytest                          # run tests
ruff check app/ tests/          # lint

# fire a test webhook locally
./scripts/test_local.sh issue
./scripts/test_local.sh pr
./scripts/test_local.sh comment
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to set up the dev environment, add a new feature, and open a PR.

---

## License

[MIT](LICENSE)

---

## Contest

Submitted to the **NVIDIA × Collabnix Open-Source Maintainer Copilot Contest** under the Community Impact — Open Source Tooling track.

---

*Authors: [Vuriti Sai Pranay](https://github.com/V-S-Pranay) · [P Suryanarayana Reddy](https://github.com/suryastark6)
