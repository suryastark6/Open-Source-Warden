"""
Open-Source-Warden launcher.

Run this instead of uvicorn directly. It:
  1. Guides the judge to install the GitHub App from the marketplace
  2. Collects any missing API keys (NVIDIA, ngrok) interactively
  3. Installs / updates ngrok if needed, starts the tunnel, captures the public URL
  4. Auto-updates the webhook URL in the GitHub App
  5. Launches the uvicorn server
"""

import io
import json
import queue
import re
import os
import sys
import time
import shutil
import subprocess
import threading
import webbrowser
import zipfile
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
ROOT        = Path(__file__).parent
ENV_FILE    = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
PEM_FILE    = ROOT / "github_private_key.pem"

MARKETPLACE_URL = "https://github.com/apps/open-source-warden"

SAFE_DEFAULTS = {
    "NVIDIA_BASE_URL":         "https://integrate.api.nvidia.com/v1",
    "NVIDIA_MODEL":            "nvidia/llama-3.3-nemotron-super-49b-v1",
    "GITHUB_APP_ID":           "3783718",
    "GITHUB_WEBHOOK_SECRET":   "V.S.Pranay",
    "GITHUB_PRIVATE_KEY_PATH": "./github_private_key.pem",
    "APP_ENV":                 "development",
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
        "description": "Exposes your local server so GitHub can deliver webhooks.",
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
# Python dependencies
# ---------------------------------------------------------------------------

def check_dependencies() -> None:
    info("Checking Python dependencies...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt"), "-q"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        warn(f"pip had issues:\n{result.stderr.strip()}")
    else:
        ok("All dependencies satisfied")

# ---------------------------------------------------------------------------
# GitHub App — marketplace install prompt
# ---------------------------------------------------------------------------

def prompt_install_github_app() -> None:
    print()
    info("GitHub App — install from marketplace")
    print(f"  The app is published at: {MARKETPLACE_URL}")
    print()
    webbrowser.open(MARKETPLACE_URL)
    print("  [browser opened]")
    print()
    print("  Steps:")
    print("   1. Click the green  'Install'  button")
    print("   2. Choose the repository you want to test on")
    print("   3. Click  'Install & Authorize'")
    print()
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
    answer = input("  Open that page in your browser right now? [Y/n]: ").strip().lower()
    if answer != "n":
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
# ngrok: find, download, configure, start
# ---------------------------------------------------------------------------

def find_ngrok() -> str | None:
    # 1. Project root (downloaded here)
    for name in ("ngrok.exe", "ngrok.EXE", "ngrok"):
        local = ROOT / name
        if local.exists():
            return str(local)
    # 2. PATH
    found = shutil.which("ngrok")
    if found:
        return found
    # 3. winget location
    local_app   = Path(os.environ.get("LOCALAPPDATA", ""))
    winget_pkgs = local_app / "Microsoft" / "WinGet" / "Packages"
    if winget_pkgs.exists():
        for folder in winget_pkgs.glob("Ngrok.Ngrok*"):
            for name in ("ngrok.exe", "ngrok.EXE"):
                exe = folder / name
                if exe.exists():
                    return str(exe)
    windows_apps = local_app / "Microsoft" / "WindowsApps" / "ngrok.exe"
    if windows_apps.exists():
        return str(windows_apps)
    return None

def _download_ngrok() -> str | None:
    url  = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip"
    dest = ROOT / "ngrok.exe"
    info("Downloading latest ngrok...")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():
                if "ngrok" in member.lower() and member.endswith(".exe"):
                    with zf.open(member) as src, open(dest, "wb") as dst:
                        dst.write(src.read())
                    ok(f"ngrok downloaded: {dest}")
                    return str(dest)
    except Exception as exc:
        err(f"Download failed: {exc}")
    return None

def ensure_ngrok() -> str:
    info("Checking ngrok...")
    exe = find_ngrok()
    if exe:
        # Verify it's new enough by doing a quick test run
        result = subprocess.run([exe, "version"], capture_output=True, text=True)
        version_line = (result.stdout + result.stderr).strip()
        ok(f"{version_line}")
        return exe
    info("ngrok not found — downloading...")
    exe = _download_ngrok()
    return exe or "ngrok"

def configure_ngrok_authtoken(ngrok_exe: str, authtoken: str) -> None:
    info("Configuring ngrok authtoken...")
    result = subprocess.run(
        [ngrok_exe, "config", "add-authtoken", authtoken],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        ok("ngrok authtoken configured")
    else:
        warn(f"ngrok config warning: {result.stderr.strip()}")

def start_ngrok_tunnel(ngrok_exe: str, port: str, authtoken: str) -> str | None:
    configure_ngrok_authtoken(ngrok_exe, authtoken)
    info(f"Starting ngrok tunnel → port {port}...")

    proc = subprocess.Popen(
        [ngrok_exe, "http", port, "--log", "stdout", "--log-format", "json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # Two parallel detectors — whichever finds the URL first wins
    found: queue.Queue = queue.Queue(maxsize=1)

    def _read_json():
        """Parse tunnel URL from ngrok's JSON log output."""
        try:
            for line in proc.stdout:
                if "ERR_NGROK" in line:
                    found.put(("error", line.strip()))
                    return
                m = re.search(r'https://[a-zA-Z0-9][a-zA-Z0-9\-]*\.ngrok[a-zA-Z0-9\-\.]*', line)
                if m and "dashboard" not in m.group(0) and "api." not in m.group(0):
                    try:
                        found.put(("url", m.group(0)))
                    except queue.Full:
                        pass
                    return
        except Exception:
            pass

    def _poll_api():
        """Poll ngrok's local dashboard API for the tunnel URL."""
        for _ in range(40):
            time.sleep(1)
            try:
                with urllib.request.urlopen("http://localhost:4040/api/tunnels", timeout=2) as resp:
                    data    = json.loads(resp.read())
                    tunnels = data.get("tunnels", [])
                    for t in tunnels:
                        pub = t.get("public_url", "")
                        if pub.startswith("https://"):
                            try:
                                found.put(("url", pub))
                            except queue.Full:
                                pass
                            return
            except Exception:
                pass

    threading.Thread(target=_read_json, daemon=True).start()
    threading.Thread(target=_poll_api,  daemon=True).start()

    url      = None
    deadline = time.time() + 42
    print("      waiting", end="", flush=True)

    while time.time() < deadline:
        try:
            kind, value = found.get(timeout=1.0)
            print()
            if kind == "url":
                url = value
            else:
                if "ERR_NGROK_121" in value or "too old" in value.lower():
                    warn("ngrok version too old — downloading latest...")
                    proc.terminate()
                    exe = _download_ngrok()
                    if exe:
                        return start_ngrok_tunnel(exe, port, authtoken)
                else:
                    err(f"ngrok error: {value}")
            break
        except queue.Empty:
            print(".", end="", flush=True)

    if not url:
        print()

    if url:
        ok(f"ngrok tunnel active: {url}")
        return url

    warn("Could not detect ngrok URL automatically.")
    print("  Check http://localhost:4040 for the tunnel URL.\n")
    while True:
        raw = input("  Paste your ngrok HTTPS URL (e.g. https://abc123.ngrok-free.app): ").strip().rstrip("/")
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
            data=body,
            method="PATCH",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept":        "application/vnd.github+json",
                "Content-Type":  "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 204):
                ok(f"Webhook URL updated → {ngrok_url}/webhook")
    except Exception:
        pass  # non-fatal

# ---------------------------------------------------------------------------
# Summary + server launch
# ---------------------------------------------------------------------------

def summary(ngrok_url: str | None) -> None:
    port        = os.environ.get("PORT", "8000")
    webhook_url = f"{ngrok_url}/webhook" if ngrok_url else "unavailable"
    print()
    print("  Configuration snapshot:")
    rows = [
        ("NVIDIA_MODEL",          os.environ.get("NVIDIA_MODEL", "?")),
        ("GITHUB_APP_ID",         os.environ.get("GITHUB_APP_ID", "?")),
        ("PORT",                  port),
        ("NVIDIA_API_KEY",        "*** set ***" if os.environ.get("NVIDIA_API_KEY") else "MISSING"),
        ("NGROK_AUTHTOKEN",       "*** set ***" if os.environ.get("NGROK_AUTHTOKEN") else "MISSING"),
        ("github_private_key.pem","found" if PEM_FILE.exists() else "MISSING"),
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

def start_server() -> None:
    port = os.environ.get("PORT", "8000")
    banner(
        f"Starting Open-Source-Warden on port {port}\n"
        f"\n"
        f"  Local  →  http://localhost:{port}\n"
        f"  Docs   →  http://localhost:{port}/docs\n"
        f"  Health →  http://localhost:{port}/health\n"
        f"  ngrok  →  http://localhost:4040\n"
        f"\n"
        f"  Press Ctrl+C to stop."
    )
    subprocess.run([
        sys.executable, "-m", "uvicorn",
        "app.main:app",
        "--host", "0.0.0.0",
        "--port", port,
        "--reload",
    ])

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    banner(
        "Open-Source-Warden  —  Environment Setup & Launcher\n"
        "\n"
        "This wizard collects your API keys, starts ngrok,\n"
        "and launches the server — all automatically."
    )

    # 1. .env
    ensure_env_file()
    env = load_env_values()
    info("Applying safe defaults...")
    env = apply_safe_defaults(env)

    # 2. Python packages
    check_dependencies()

    # 3. Install GitHub App from marketplace
    prompt_install_github_app()

    # 4. API keys
    print()
    info("Checking required API keys and tokens...")
    for secret in REQUIRED_SECRETS:
        prompt_for_secret(secret, dotenv_values(ENV_FILE))

    # 5. ngrok
    print()
    info("Setting up ngrok tunnel...")
    ngrok_exe = ensure_ngrok()
    authtoken = os.environ.get("NGROK_AUTHTOKEN", "")
    port      = os.environ.get("PORT", "8000")
    ngrok_url = start_ngrok_tunnel(ngrok_exe, port, authtoken)

    # 6. Auto-update webhook URL
    if ngrok_url:
        info("Updating webhook URL in GitHub App...")
        update_webhook_url(ngrok_url)

    # 7. Summary
    summary(ngrok_url)

    input("  Press Enter to start the server...")

    # 8. uvicorn
    start_server()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Interrupted. Goodbye.")
        sys.exit(0)
