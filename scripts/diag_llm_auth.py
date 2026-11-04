r"""Diagnose ``llm provider error: unauthorized`` for senza-agent.

Prints every credential source senza-agent may read, the effective config it
will actually use, then performs the same two calls the app performs
(raw /models probe and a real provider chat).  The fingerprint (length + sha256
prefix) of each key is shown instead of the key itself, so two different keys,
or a key with a stray space/CR/quote, are immediately visible.

Usage (Windows, packaged app):

    $inst = "$env:LOCALAPPDATA\Programs\SenzaAgent"
    $env:PYTHONPATH = "$inst\resources"
    & "$inst\python_venv\Scripts\python.exe" "$env:USERPROFILE\diag_llm_auth.py"

Dev checkout:

    .venv/bin/python scripts/diag_llm_auth.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

KEY_ENV = [
    "OPENAI_API_KEY",
    "OPENAI_OPENAI_API_KEY",
    "BACKUP_OPENAI_API_KEY",
    "BACKUP2_OPENAI_API_KEY",
    "ADVISOR1_OPENAI_API_KEY",
    "ADVISOR2_OPENAI_API_KEY",
    "SENZA_AGENT_API_KEY",
]
OTHER_ENV = [
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_OPENAI_BASE_URL",
    "BACKUP_OPENAI_BASE_URL",
    "BACKUP2_OPENAI_BASE_URL",
    "SENZA_AGENT_API_BASE",
    "OPENAI_MODEL",
    "OPENAI_OPENAI_MODEL",
    "SENZA_AGENT_MODEL",
    "SENZA_AGENT_PROVIDER_TYPE",
    "PREFERRED_API",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
]


def fp(value: str | None) -> str:
    """Irreversible fingerprint: enough to tell two keys apart, not to leak one."""
    if value is None:
        return "<unset>"
    if value == "":
        return "<empty>  (blank; <=0.1.9 let this shadow settings.json)"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    visible = value[:6] + "…" + value[-4:] if len(value) > 12 else value
    extra = ""
    if value != value.strip():
        extra += " [HAS LEADING/TRAILING WHITESPACE]"
    if any(ord(c) < 32 for c in value):
        extra += " [HAS CONTROL CHARS]"
    return f"len={len(value)} sha256={digest} {visible}{extra}"


def show_env() -> None:
    print("── environment (process) ─────────────────────────────────────────")
    for name in KEY_ENV:
        print(f"  {name:26} {fp(os.environ.get(name))}")
    for name in OTHER_ENV:
        val = os.environ.get(name)
        if val is None:
            if name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
                continue
            print(f"  {name:26} <unset>")
        elif val == "":
            print(f"  {name:26} <empty>")
        else:
            print(f"  {name:26} {val}")
    for path in (".env",):
        if os.path.isfile(path):
            print(f"  NOTE: {os.path.abspath(path)} exists and is loaded before settings.json")


def show_files() -> None:
    from senza_agent import config as cfgmod

    print("\n── credential files ──────────────────────────────────────────────")
    for label, path in (("settings.json", cfgmod._settings_file()),
                        ("config.json", cfgmod._config_file())):
        print(f"  {label:14} {path}", "exists" if path.is_file() else "MISSING")
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - diagnostic path
            print(f"    <unreadable: {exc}>")
            continue
        if not isinstance(data, dict):
            continue
        for key in KEY_ENV:
            if key in data:
                print(f"    {key:24} {fp(str(data[key]))}")
        if "api_key" in data:
            print(f"    {'api_key':24} {fp(str(data['api_key']))}")
        for key in ("OPENAI_BASE_URL", "api_base", "OPENAI_MODEL", "model",
                    "provider_type"):
            if key in data:
                print(f"    {key:24} {data[key]}")
        for section, sub in data.items():
            if not isinstance(sub, dict):
                continue
            for k, v in sub.items():
                shown = fp(str(v)) if "key" in k.lower() else v
                print(f"    {section + '.' + k:24} {shown}")


def show_effective() -> dict:
    from senza_agent.config import load_config

    cfg = load_config()
    print("\n── effective config (what the running agent uses) ─────────────────")
    print(f"  model         {cfg.model}")
    print(f"  api_base      {cfg.api_base}")
    print(f"  provider_type {cfg.provider_type}")
    print(f"  api_key       {fp(cfg.api_key)}")

    # senza_agent.config._apply_env resolves these with this precedence.  Any
    # of them silently overrides what the dashboard shows / saves (the
    # dashboard only reads and writes OPENAI_BASE_URL and OPENAI_API_KEY).
    base_pref = ["SENZA_AGENT_API_BASE", "OPENAI_BASE_URL", "OPENAI_API_BASE"]
    key_pref = ["SENZA_AGENT_API_KEY", "OPENAI_API_KEY"]
    base_set = [n for n in base_pref if os.environ.get(n)]
    key_set = [n for n in key_pref if os.environ.get(n)]
    print(f"  api_base from {base_set[0] if base_set else '<none — empty>'}"
          f"  (set: {', '.join(base_set) or 'none'})")
    print(f"  api_key  from {key_set[0] if key_set else '<none — empty>'}"
          f"  (set: {', '.join(key_set) or 'none'})")
    dashboard_base = os.environ.get("OPENAI_BASE_URL", "")
    if dashboard_base and cfg.api_base != dashboard_base:
        print(f"  !! WARNING: dashboard/dotenv says {dashboard_base} but the "
              f"runtime uses {cfg.api_base} ({base_set[0]} wins)")
    return cfg


def probe_raw(cfg) -> None:
    import urllib.error
    import urllib.request

    base = (cfg.api_base or "").rstrip("/")
    if not base:
        print("\n[raw probe] skipped: api_base is empty")
        return
    # senza.providers.openai() default chat_path is "/v1/chat/completions" and
    # it normalises a base_url that already ends in "/v1".
    url = base + ("/chat/completions" if base.endswith("/v1")
                  else "/v1/chat/completions")
    body = json.dumps({
        "model": cfg.model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 4,
    }).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {cfg.api_key}",
                 "Content-Type": "application/json"},
    )
    print(f"\n[raw probe] POST {url}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            print(f"  HTTP {resp.status}: {resp.read(200)!r}")
    except urllib.error.HTTPError as exc:
        print(f"  HTTP {exc.code}: {exc.read(400)!r}")
    except Exception as exc:  # noqa: BLE001 - diagnostic path
        print(f"  {type(exc).__name__}: {exc}")


def probe_runtime(cfg) -> None:
    import asyncio

    import senza

    from senza_agent.config import create_provider

    print("\n[runtime probe] same call the agent makes")

    async def run() -> None:
        provider = create_provider(cfg)
        harness = (
            senza.HarnessBuilder(cfg.model)
            .provider("*", provider)
            .build()
        )
        text = await harness.chat_async("hi", timeout_ms=30000)
        print(f"  OK: {text[:60]!r}")

    try:
        asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - diagnostic path
        print(f"  FAIL: {type(exc).__name__}: {exc}")


def hydrate_like_startup() -> None:
    """Replicate ``cli.main()`` steps 2–3b before inspecting anything.

    Without this the script only read config.json + the *inherited* process
    environment, so a machine whose credentials live solely in
    ``~/.senza-agent/settings.json`` (the normal dashboard setup) was reported
    as ``api_key=<empty> api_base=<empty>`` — a false negative that also made
    both probes skip.
    """
    try:
        from senza_agent.cli import load_dotenv_if_present

        load_dotenv_if_present()
    except Exception as exc:  # noqa: BLE001 - diagnostic path
        print(f"  <dotenv load failed: {exc}>")
    try:
        from senza_agent.config import load_settings_into_env

        load_settings_into_env()
    except Exception as exc:  # noqa: BLE001 - diagnostic path
        print(f"  <settings.json load failed: {exc}>")
    try:
        from senza_agent.cli import ensure_env_defaults

        ensure_env_defaults()
    except Exception as exc:  # noqa: BLE001 - diagnostic path
        print(f"  <env defaults failed: {exc}>")


def main() -> int:
    print(f"python  {sys.version.split()[0]} ({sys.executable})")
    print(f"cwd     {os.getcwd()}")
    try:
        import senza_agent

        print(f"agent   {senza_agent.__version__} ({senza_agent.__file__})")
    except Exception as exc:  # noqa: BLE001 - diagnostic path
        print(f"agent   <import failed: {exc}>")
    try:
        import senza
        print(f"senza   {senza.version()}")
    except Exception as exc:  # noqa: BLE001 - diagnostic path
        print(f"senza   <import failed: {exc}>")
    print("── startup hydration (same order as cli.main) ────────────────────")
    hydrate_like_startup()
    show_env()
    show_files()
    cfg = show_effective()
    probe_raw(cfg)
    probe_runtime(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
