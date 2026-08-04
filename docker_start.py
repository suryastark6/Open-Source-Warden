"""
Open-Source-Warden — Docker launcher.

Collects credentials, writes .env, then runs:
    docker compose --profile dev up --build

Both the app and ngrok run inside Docker (via docker-compose.yml).
The ngrok tunnel URL is fetched from localhost:4040 and the
webhook URL is auto-updated in the GitHub App.
"""

import json
import os
import sys
import time
import shutil
import subprocess
import threading
import webbrowser
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap python-dotenv
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv, set_key, dotenv_values
except ImportError:
    print("[setup] python-dotenv not found — installing...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "python-dotenv", "-q"])
    from dotenv import load_dotenv, set_key, dotenv_values

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ROOT            = Path(__file__).parent
ENV_FILE        = ROOT / ".env"
ENV_EXAMPLE     = ROOT / ".env.example"
PEM_FILE        = ROOT / "github_private_key.pem"
MARKETPLACE_URL = "https://github.com/apps/open-source-warden"

SAFE_DEFAULTS = {
    "NVIDIA_BASE_URL":         "https://integrate.api.nvidia.com/v1",
    "NVIDIA_MODEL":            "nvidia/llama-3.3-nemotron-super-49b-v1",
    "GITHUB_APP_ID":           "3783718",
    "GITHUB_WEBHOOK_SECRET":   "V.S.Pranay",
    "GITHUB_PRIVATE_KEY_PATH": "./github_private_key.pem",  # relative — works on host AND inside container at /app/
    "APP_ENV":                 "production",
    "LOG_LEVEL":               "INFO",
    "PORT":                    "8000",
    "FEATURE_TRIAGE":          "true",
    "FEATURE_REPRODUCTION":    "true",
    "FEATURE_ONBOARDING":      "true",
    "FEATURE_PR_REVIEW":       "true",
    "FEATURE_RELEASE_NOTES":   "true",
}

REQUIRED_SECRETS = [
    {
        "key":         "NVIDIA_API_KEY",
        "label":       "NVIDIA NIM API Key",
        "description": "Lets the app call the Nemotron AI model for every analysis.",
        "url":         "https://build.nvidia.com",
        "instructions": (
            "1. Sign up / log in at build.nvidia.com\n"
            "   2. Click on any model (e.g. Nemotron)\n"
            "   3. Click 'Get API Key' in the top-right\n"
            "   4. Copy the key — it starts with  nvapi-"
        ),
        "validate": lambda v: v.startswith("nvapi-") and len(v) > 30,
        "hint":     "Must start with 'nvapi-'",
    },
    {
        "key":         "NGROK_AUTHTOKEN",
        "label":       "ngrok Auth Token",
        "description": "Used by the ngrok container to create a public tunnel.",
        "url":         "https://dashboard.ngrok.com/get-started/your-authtoken",
        "instructions": (
            "1. Sign up / log in at ngrok.com\n"
            "   2. Go to Dashboard → Your Authtoken\n"
            "   3. Copy the token"
        ),
        "validate": lambda v: len(v) > 20,
        "hint":     "Token looks too short — double-check what you pasted",
    },
]

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def banner(text: str, width: int = 62) -> None:
    print("\n" + "=" * width)
    for line in text.splitlines():
        print(f"  {line}")
    print("=" * width + "\n")

def ok(msg: str)   -> None: print(f"  [ok]    {msg}")
def info(msg: str) -> None: print(f"  [info]  {msg}")
def warn(msg: str) -> None: print(f"  [warn]  {msg}")
def err(msg: str)  -> None: print(f"  [error] {msg}")

# ---------------------------------------------------------------------------
# .env helpers
# ---------------------------------------------------------------------------

def ensure_env_file() -> None:
    if ENV_FILE.exists():
        return
    if ENV_EXAMPLE.exists():
        shutil.copy(ENV_EXAMPLE, ENV_FILE)
        info(".env created from .env.example")
    else:
        ENV_FILE.touch()
        info(".env created (empty)")

def load_env_values() -> dict:
    load_dotenv(ENV_FILE, override=True)
    return dotenv_values(ENV_FILE)

def apply_safe_defaults(current: dict) -> dict:
    wrote_any = False
    for key, value in SAFE_DEFAULTS.items():
        if not current.get(key, "").strip():
            set_key(str(ENV_FILE), key, value)
            os.environ[key] = value
            wrote_any = True
    if wrote_any:
        return dotenv_values(ENV_FILE)
    return current

def save_env(key: str, value: str) -> None:
    set_key(str(ENV_FILE), key, value)
    os.environ[key] = value

# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_docker() -> None:
    info("Checking Docker...")
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, text=True)
        if result.returncode == 0:
            ok("Docker is running")
            return
        # Docker found but not running
        err("Docker is installed but not running.")
        print("  Please start Docker Desktop and press Enter to retry.")
        input("  Press Enter when Docker Desktop is running... ")
        check_docker()
    except FileNotFoundError:
        err("Docker is not installed.")
        print()
        print("  Docker Desktop is required for this launch method.")
        print("  Download it from: https://www.docker.com/products/docker-desktop/")
        print()
        webbrowser.open("https://www.docker.com/products/docker-desktop/")
        print("  [browser opened]")
        print()
        print("  After installing and starting Docker Desktop,")
        print("  re-run this script:  python docker_start.py")
        print()
        print("  Alternatively, use the non-Docker launcher:")
        print("    python start.py")
        print()
        sys.exit(1)

def check_pem() -> None:
    if PEM_FILE.exists():
        ok("github_private_key.pem found")
        return
    print()
    warn("github_private_key.pem is missing!")
    print(f"  Place it in: {ROOT}")
    while not PEM_FILE.exists():
        input("  Press Enter once you have placed the file... ")
        if not PEM_FILE.exists():
            warn("Still not found — check the filename.")
    ok("github_private_key.pem found")

# ---------------------------------------------------------------------------
# GitHub App marketplace prompt
# ---------------------------------------------------------------------------

def prompt_install_github_app() -> None:
    print()
    info("GitHub App — install from marketplace")
    print(f"  The app is published at: {MARKETPLACE_URL}")
    webbrowser.open(MARKETPLACE_URL)
    print("  [browser opened]\n")
    print("  Steps:")
    print("   1. Click the green  'Install'  button")
    print("   2. Choose the repository you want to test on")
    print("   3. Click  'Install & Authorize'\n")
    input("  Press Enter once you have installed the app on your test repo... ")
    ok("GitHub App install confirmed")

# ---------------------------------------------------------------------------
# Interactive secret prompts
# ---------------------------------------------------------------------------

def prompt_for_secret(secret: dict, current: dict) -> None:
    key   = secret["key"]
    value = current.get(key, "").strip()
    if value and secret["validate"](value):
        ok(f"{key} already set")
        return
    print()
    print(f"  {'─' * 58}")
    print(f"  MISSING  →  {secret['label']}")
    print(f"  {'─' * 58}")
    print(f"  What it does : {secret['description']}")
    print(f"  Where to get : {secret['url']}")
    print(f"\n  Steps:\n   {secret['instructions']}\n")
    if input("  Open that page in your browser right now? [Y/n]: ").strip().lower() != "n":
        webbrowser.open(secret["url"])
        print("  [browser opened] Take your time — come back when you have the value.")
        time.sleep(1)
    while True:
        value = input(f"\n  Paste your {secret['label']}: ").strip()
        if not value:
            warn("Cannot be empty — try again.")
            continue
        if secret["validate"](value):
            save_env(secret["key"], value)
            ok(f"{key} saved to .env")
            break
        warn(f"Invalid — {secret['hint']}. Try again.")

# ---------------------------------------------------------------------------
# Docker Compose
# ---------------------------------------------------------------------------

def compose_up() -> None:
    info("Building images and starting containers (app + ngrok)...")
    print("  This may take a minute on first run.\n")
    result = subprocess.run(
        ["docker", "compose", "--profile", "dev", "up", "--build", "-d"],
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        err("docker compose failed.")
        sys.exit(1)
    ok("Containers started")

def wait_for_app(port: str) -> None:
    info("Waiting for the app container to be healthy...")
    print("      waiting", end="", flush=True)
    for _ in range(30):
        time.sleep(2)
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=2) as resp:
                if resp.status == 200:
                    print()
                    ok(f"App is healthy at http://localhost:{port}")
                    return
        except Exception:
            print(".", end="", flush=True)
    print()
    warn("App health check timed out — continuing anyway.")

# ---------------------------------------------------------------------------
# Get ngrok URL from its container dashboard API
# ---------------------------------------------------------------------------

def get_ngrok_url(retries: int = 30, delay: float = 2.0) -> str | None:
    info("Waiting for ngrok tunnel to be ready...")
    print("      waiting", end="", flush=True)
    for _ in range(retries):
        time.sleep(delay)
        try:
            with urllib.request.urlopen("http://localhost:4040/api/tunnels", timeout=2) as resp:
                data    = json.loads(resp.read())
                tunnels = data.get("tunnels", [])
                for t in tunnels:
                    pub = t.get("public_url", "")
                    if pub.startswith("https://"):
                        print()
                        ok(f"ngrok tunnel active: {pub}")
                        return pub
        except Exception:
            print(".", end="", flush=True)
    print()
    warn("Could not fetch ngrok URL from localhost:4040")
    while True:
        raw = input("  Paste your ngrok HTTPS URL manually: ").strip().rstrip("/")
        if not raw:
            continue
        if "ngrok" in raw:
            return raw if raw.startswith("https://") else "https://" + raw[len("http://"):]
        warn("Paste the full https://... URL.")

# ---------------------------------------------------------------------------
# Auto-update webhook URL in the GitHub App
# ---------------------------------------------------------------------------

def update_webhook_url(ngrok_url: str) -> None:
    app_id = os.environ.get("GITHUB_APP_ID", "")
    if not app_id or not PEM_FILE.exists():
        return
    try:
        import jwt as pyjwt
        pem   = PEM_FILE.read_text()
        now   = int(time.time())
        token = pyjwt.encode(
            {"iat": now - 60, "exp": now + 300, "iss": app_id},
            pem, algorithm="RS256",
        )
        body = json.dumps({
            "webhook_attributes": {"url": f"{ngrok_url}/webhook", "active": True}
        }).encode()
        req = urllib.request.Request(
            "https://api.github.com/app/hook/config",
            data=body, method="PATCH",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept":        "application/vnd.github+json",
                "Content-Type":  "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 204):
                ok(f"Webhook URL updated → {ngrok_url}/webhook")
    except Exception as exc:
        warn(f"Could not auto-update webhook URL: {exc}")
        warn(f"Set it manually in GitHub App settings: {ngrok_url}/webhook")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def summary(ngrok_url: str | None) -> None:
    port        = os.environ.get("PORT", "8000")
    webhook_url = f"{ngrok_url}/webhook" if ngrok_url else "unavailable"
    print()
    print("  All systems running inside Docker:\n")
    rows = [
        ("App container",         "open-source-warden"),
        ("ngrok container",       "ngrok-tunnel"),
        ("App URL",               f"http://localhost:{port}"),
        ("ngrok dashboard",       "http://localhost:4040"),
        ("NVIDIA_MODEL",          os.environ.get("NVIDIA_MODEL", "?")),
        ("GITHUB_APP_ID",         os.environ.get("GITHUB_APP_ID", "?")),
        ("Webhook URL",           webhook_url),
    ]
    for label, val in rows:
        print(f"    {label:<28} {val}")
    print()
    if ngrok_url:
        print("  ┌─────────────────────────────────────────────────────────────┐")
        print(f"  │  Webhook URL (auto-configured in your GitHub App):          │")
        print(f"  │                                                             │")
        print(f"  │    {webhook_url:<57}│")
        print(f"  │                                                             │")
        print("  └─────────────────────────────────────────────────────────────┘")
    print()
    print("  Useful commands:")
    print("    docker compose --profile dev logs -f    ← live logs from all containers")
    print("    docker compose --profile dev down       ← stop everything")
    print("    http://localhost:4040                   ← ngrok dashboard")
    print()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    banner(
        "Open-Source-Warden  —  Docker Setup & Launcher\n"
        "\n"
        "Configures credentials, builds Docker images, and starts\n"
        "both the app and ngrok tunnel inside Docker containers."
    )

    # 1. .env
    ensure_env_file()
    env = load_env_values()
    info("Applying safe defaults...")
    apply_safe_defaults(env)

    # 2. Install host-side Python deps (needed for JWT in update_webhook_url)
    info("Checking Python dependencies...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt"), "-q"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        ok("Python dependencies ready")
    else:
        warn(f"pip had issues: {result.stderr.strip()}")

    # 3. Docker check
    check_docker()

    # 3. GitHub App
    prompt_install_github_app()

    # 4. PEM file
    print()
    info("Checking GitHub App private key...")
    check_pem()

    # 5. API keys
    print()
    info("Checking required API keys and tokens...")
    for secret in REQUIRED_SECRETS:
        prompt_for_secret(secret, dotenv_values(ENV_FILE))

    # 6. Build + run everything via docker compose
    print()
    compose_up()

    # 7. Wait for app health
    port = os.environ.get("PORT", "8000")
    wait_for_app(port)

    # 8. Get ngrok URL from the ngrok container's dashboard API
    print()
    ngrok_url = get_ngrok_url()

    # 9. Update webhook URL in GitHub App
    if ngrok_url:
        info("Updating webhook URL in GitHub App...")
        update_webhook_url(ngrok_url)

    # 10. Summary
    summary(ngrok_url)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Interrupted.")
        print("  Containers are still running. To stop them:")
        print("    docker compose --profile dev down")
        sys.exit(0)
