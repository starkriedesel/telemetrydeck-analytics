#!/usr/bin/env python3
"""TelemetryDeck v3 query CLI — portable, OS-native secret storage.

No `.env` needed. Credentials live in the OS-native secret store when one is
available:
  - macOS:   Keychain via `security`
  - Linux:   libsecret via `secret-tool` (if installed)
  - Windows / Linux without secret-tool: file fallback with mode 0600
Non-secret state (email, registered apps, token expiry) lives in a
platform-appropriate config directory (`~/Library/Application Support/` on
macOS, `$XDG_CONFIG_HOME` or `~/.config/` on Linux, `%APPDATA%\\` on Windows).

Subcommands:
  login                 Prompt for email/password (or --pat), auto-discover apps, pick one.
  logout                Clear stored secrets and config file.
  whoami                Verify token, show user/org info.
  apps                  List / add / remove / switch registered apps.
  insights              List saved insights for the current app.
  insight <id>          Run a saved insight and print results.
  query <file|->        Run a raw TQL query from file or stdin.
  signals               Convenience: top events over last N days.

Auth resolution order (per invocation):
  1. Stored bearer, if present and not within 5 min of expiry. A personal
     access token stored by `login --pat` lives here and has no expiry, so it
     is used until the API rejects it.
  2. Stored password → POST /api/v3/users/login → new bearer.
  3. On HTTP 401, step 2 once, then retry.
  4. If (2) has no password, interactive prompt; stash in secret store.
     SSO accounts have no password and must use `login --pat`.

App UUID resolution:
  1. --app-id flag
  2. TELEMETRYDECK_APP_ID env var
  3. config.json `current_app_id` (the app `login` / `apps use` selected)
  4. Interactive prompt on first use, saved to config.

Never prints the password, bearer, or any secret-store contents to stdout.

Usage:
  python3 tdq.py login
  python3 tdq.py apps                 # list registered apps
  python3 tdq.py apps use <uuid|name> # switch current app
  python3 tdq.py signals --days 30 --top 25
  python3 tdq.py insight <uuid> --days 30
  cat tql.json | python3 tdq.py query -
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API_BASE = "https://api.telemetrydeckapi.com"
SECRET_SERVICE = "telemetrydeck-cli"  # service/label name in the secret store
SECRET_ACCT_PASSWORD = "password"
SECRET_ACCT_TOKEN = "token"

AUTO_FILTER_SENTINEL = "__auto_app_and_test_mode_filter__"


# ---------- Platform-aware paths ----------

def _config_dir() -> Path:
    """Return the OS-appropriate config directory (created on demand)."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TelemetryDeckCLI"
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "TelemetryDeckCLI"
    # Linux, *BSD, etc. — XDG
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "TelemetryDeckCLI"


CONFIG_DIR = _config_dir()
CONFIG_PATH = CONFIG_DIR / "config.json"
SECRET_FILE_PATH = CONFIG_DIR / "secrets.json"  # fallback store


# ---------- Secret storage (OS-native where possible, file fallback) ----------

def _is_macos() -> bool:
    return sys.platform == "darwin"


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _has_secret_tool() -> bool:
    return _is_linux() and shutil.which("secret-tool") is not None


def _run(cmd: list[str], input_: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, input=input_, capture_output=True, text=True, check=False)


def secret_backend() -> str:
    """Identify the active secret backend. One of `keychain`, `secret-tool`, `file`."""
    if _is_macos():
        return "keychain"
    if _has_secret_tool():
        return "secret-tool"
    return "file"


_FILE_WARNED = False


def _file_warn_once() -> None:
    """Emit a single stderr warning the first time the file backend is used."""
    global _FILE_WARNED
    if _FILE_WARNED:
        return
    _FILE_WARNED = True
    sys.stderr.write(
        f"WARN: no OS keychain available on this platform — secrets stored in\n"
        f"      {SECRET_FILE_PATH} (mode 0600). For stronger protection,\n"
        f"      install libsecret (`secret-tool`) on Linux or set\n"
        f"      TELEMETRYDECK_PASSWORD / TELEMETRYDECK_TOKEN env vars.\n"
    )


def _file_store_read() -> dict[str, str]:
    if not SECRET_FILE_PATH.exists():
        return {}
    try:
        return json.loads(SECRET_FILE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _file_store_write(data: dict[str, str]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SECRET_FILE_PATH.write_text(json.dumps(data, indent=2, sort_keys=True))
    try:
        os.chmod(SECRET_FILE_PATH, 0o600)
    except OSError:
        pass


def secret_get(account: str) -> str | None:
    """Read a secret. Returns None if missing. Never raises on platform mismatch."""
    backend = secret_backend()
    if backend == "keychain":
        r = _run([
            "security", "find-generic-password",
            "-s", SECRET_SERVICE, "-a", account, "-w",
        ])
        if r.returncode != 0:
            return None
        return r.stdout.rstrip("\n")
    if backend == "secret-tool":
        r = _run([
            "secret-tool", "lookup",
            "service", SECRET_SERVICE, "account", account,
        ])
        if r.returncode != 0 or not r.stdout:
            return None
        return r.stdout.rstrip("\n")
    # file
    _file_warn_once()
    return _file_store_read().get(account)


def secret_set(account: str, value: str) -> None:
    """Store a secret under `(SECRET_SERVICE, account)` using the active backend."""
    backend = secret_backend()
    if backend == "keychain":
        r = _run([
            "security", "add-generic-password",
            "-U",                       # update if exists
            "-s", SECRET_SERVICE,
            "-a", account,
            "-w", value,
            "-T", "",                   # no app allowed to access without prompt
        ])
        if r.returncode != 0:
            sys.exit(f"Keychain write failed: {r.stderr.strip()}")
        return
    if backend == "secret-tool":
        # secret-tool reads the password from stdin with `store`.
        r = _run(
            [
                "secret-tool", "store",
                "--label", f"{SECRET_SERVICE}:{account}",
                "service", SECRET_SERVICE, "account", account,
            ],
            input_=value,
        )
        if r.returncode != 0:
            sys.exit(f"secret-tool write failed: {r.stderr.strip()}")
        return
    # file
    _file_warn_once()
    data = _file_store_read()
    data[account] = value
    _file_store_write(data)


def secret_delete(account: str) -> None:
    """Remove a secret. No-op if missing."""
    backend = secret_backend()
    if backend == "keychain":
        _run(["security", "delete-generic-password", "-s", SECRET_SERVICE, "-a", account])
        return
    if backend == "secret-tool":
        _run(["secret-tool", "clear", "service", SECRET_SERVICE, "account", account])
        return
    # file
    data = _file_store_read()
    if account in data:
        data.pop(account)
        if data:
            _file_store_write(data)
        else:
            try:
                SECRET_FILE_PATH.unlink()
            except OSError:
                pass


# ---- Backward-compat aliases (older call sites used `keychain_*`) ----
keychain_get = secret_get
keychain_set = secret_set
keychain_delete = secret_delete
KEYCHAIN_SERVICE = SECRET_SERVICE
KEYCHAIN_ACCT_PASSWORD = SECRET_ACCT_PASSWORD
KEYCHAIN_ACCT_TOKEN = SECRET_ACCT_TOKEN


# ---------- Config ----------

def load_config() -> dict[str, Any]:
    """Read config.json and transparently migrate old single-app schema.

    Old shape: {"app_id": "<uuid>", ...}
    New shape: {"apps": {"<uuid>": "<name>"}, "current_app_id": "<uuid>", ...}
    """
    if not CONFIG_PATH.exists():
        return {}
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError:
        return {}
    # Migrate legacy `app_id` scalar → `apps` dict + `current_app_id`.
    legacy = cfg.pop("app_id", None)
    if legacy:
        apps = cfg.get("apps") or {}
        apps.setdefault(legacy, apps.get(legacy) or "app")
        cfg["apps"] = apps
        cfg.setdefault("current_app_id", legacy)
        save_config(cfg)  # persist the migration
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, sort_keys=True))
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def _apps_dict(cfg: dict[str, Any]) -> dict[str, str]:
    """Return the registered-apps mapping (uuid → display name), creating if missing."""
    apps = cfg.get("apps")
    if not isinstance(apps, dict):
        apps = {}
        cfg["apps"] = apps
    return apps


def register_app(cfg: dict[str, Any], app_id: str, name: str | None = None) -> None:
    """Add an app to the config (or update its name). Does not change `current_app_id`."""
    apps = _apps_dict(cfg)
    apps[app_id] = name or apps.get(app_id) or "app"


def set_current_app(cfg: dict[str, Any], app_id: str) -> None:
    """Switch the `current_app_id` and ensure the app is registered."""
    register_app(cfg, app_id)
    cfg["current_app_id"] = app_id


def resolve_app_selector(cfg: dict[str, Any], selector: str) -> str | None:
    """Resolve a user-typed app selector to a UUID.

    Accepts: full UUID, registered display name, or 1-based index into the
    sorted list of registered apps. Returns None if nothing matches.
    """
    apps = _apps_dict(cfg)
    if selector in apps:
        return selector
    # Name match (case-insensitive, exact)
    lowered = selector.lower()
    for uuid, name in apps.items():
        if (name or "").lower() == lowered:
            return uuid
    # Positional index into sorted name list
    try:
        idx = int(selector)
        ordered = sorted(apps.items(), key=lambda kv: (kv[1] or "", kv[0]))
        if 1 <= idx <= len(ordered):
            return ordered[idx - 1][0]
    except ValueError:
        pass
    return None


# ---------- Auth ----------

def _parse_expiry(iso: str | None) -> dt.datetime | None:
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def _token_still_fresh(cfg: dict[str, Any]) -> str | None:
    token = keychain_get(KEYCHAIN_ACCT_TOKEN) or os.environ.get("TELEMETRYDECK_TOKEN")
    if not token:
        return None
    exp = _parse_expiry(cfg.get("token_expires_at"))
    if exp is None:
        return token  # No expiry info; trust it, let 401 trigger refresh.
    now = dt.datetime.now(dt.timezone.utc)
    if (exp - now).total_seconds() > 300:
        return token
    return None


def _store_pat(cfg: dict[str, Any], value: str) -> str:
    """Validate a personal access token, store it, and record the account email.

    The token may come from the flag's value, stdin (when piped), or a hidden
    prompt. Accounts that sign in with SSO have no password to exchange for a
    bearer, so this is their only route to an authenticated CLI.
    """
    token = value.strip()
    if not token:
        token = (getpass.getpass("TelemetryDeck personal access token: ")
                 if sys.stdin.isatty() else sys.stdin.read()).strip()
    if not token:
        sys.exit("Personal access token required.")

    try:
        info = http("GET", "/api/v3/users/info", token=token)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code in (401, 403):
            sys.exit(
                f"Token rejected ({e.code}). Check it was copied whole and has "
                f"not been revoked. Personal access tokens also require a paid "
                f"plan.\n{body}"
            )
        sys.exit(f"Token check failed ({e.code}): {body}")
    except urllib.error.URLError as e:
        sys.exit(f"Token check network error: {e}")

    secret_set(SECRET_ACCT_TOKEN, token)
    if info.get("email"):
        cfg["email"] = info["email"]
    # An expiry left by an earlier password login would force a re-mint of a
    # token that never expires on that schedule.
    cfg.pop("token_expires_at", None)
    save_config(cfg)
    return token


def _no_password_hint() -> str:
    """Message for the password dead end, which SSO accounts always hit."""
    stored = "A token is stored but was rejected. " if secret_get(SECRET_ACCT_TOKEN) else ""
    return (
        f"{stored}No password on file. Run: tdq login\n"
        f"If your account signs in with Google/SSO it has no password — "
        f"use a personal access token instead: tdq login --pat"
    )


def _mint_token(cfg: dict[str, Any], *, interactive: bool) -> str:
    email = cfg.get("email") or os.environ.get("TELEMETRYDECK_EMAIL")
    password = keychain_get(KEYCHAIN_ACCT_PASSWORD) or os.environ.get("TELEMETRYDECK_PASSWORD")

    if not email:
        if not interactive:
            sys.exit("No email on file. Run: tdq login")
        if not sys.stdin.isatty():
            sys.exit(
                "stdin is not a TTY — cannot prompt for email.\n"
                "Run `tdq login` directly in your terminal."
            )
        email = input("TelemetryDeck email: ").strip()
        if not email:
            sys.exit("Email required.")
        cfg["email"] = email

    if not password:
        if not interactive:
            sys.exit(_no_password_hint())
        if not sys.stdin.isatty():
            sys.exit(
                "stdin is not a TTY — cannot prompt for password.\n"
                "Run `tdq login` directly in your terminal, or authenticate "
                "with a personal access token: `tdq login --pat`."
            )
        password = getpass.getpass("TelemetryDeck password: ")
        if not password:
            sys.exit("Password required.")
        keychain_set(KEYCHAIN_ACCT_PASSWORD, password)

    basic = base64.b64encode(f"{email}:{password}".encode()).decode()
    req = urllib.request.Request(
        f"{API_BASE}/api/v3/users/login",
        method="POST",
        headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code in (401, 403):
            # Stored password is bad — clear and surface it.
            keychain_delete(KEYCHAIN_ACCT_PASSWORD)
            sys.exit(
                f"Login rejected ({e.code}). Stored password cleared. "
                f"Run: tdq.py login"
            )
        sys.exit(f"Login failed ({e.code}): {body}")
    except urllib.error.URLError as e:
        sys.exit(f"Login network error: {e}")

    token = data.get("value")
    if not token:
        sys.exit(f"Login response missing token. Payload: {data}")

    keychain_set(KEYCHAIN_ACCT_TOKEN, token)
    cfg["token_expires_at"] = data.get("expiresAt")
    save_config(cfg)
    return token


def get_token(*, force_refresh: bool = False, interactive: bool = False) -> str:
    cfg = load_config()
    if not force_refresh:
        fresh = _token_still_fresh(cfg)
        if fresh:
            return fresh
    return _mint_token(cfg, interactive=interactive)


# ---------- App ID ----------

def get_app_id(args: argparse.Namespace, *, interactive: bool = False) -> str:
    """Resolve the app UUID for this invocation.

    Resolution order: `--app-id` flag → `TELEMETRYDECK_APP_ID` env → config
    `current_app_id` → interactive prompt (if allowed). Interactive prompt
    offers the list of registered apps if any exist.
    """
    explicit = getattr(args, "app_id", None) or os.environ.get("TELEMETRYDECK_APP_ID")
    if explicit:
        return explicit
    cfg = load_config()
    current = cfg.get("current_app_id")
    if current:
        return current
    if not interactive:
        sys.exit(
            "No app selected. Pass --app-id, set TELEMETRYDECK_APP_ID, "
            "or run: tdq.py login   (or: tdq.py apps use <uuid>)"
        )
    # No registered apps — prompt.
    apps = _apps_dict(cfg)
    if apps:
        print("Registered apps:")
        for uuid, name in sorted(apps.items(), key=lambda kv: (kv[1] or "", kv[0])):
            print(f"  {name}  ({uuid})")
    if not sys.stdin.isatty():
        sys.exit(
            "stdin is not a TTY — cannot prompt for app UUID.\n"
            "Pass --app-id <UUID> or run `tdq apps use <uuid>` in your terminal."
        )
    app_id = input("TelemetryDeck app UUID to use: ").strip()
    if not app_id:
        sys.exit("App ID required.")
    set_current_app(cfg, app_id)
    save_config(cfg)
    return app_id


# ---------- App discovery via API ----------

def discover_apps() -> list[dict[str, str]]:
    """Fetch the authenticated user's apps from the TelemetryDeck API.

    Tries several plausible endpoints since the v3 surface is still settling.
    Returns `[{"id": uuid, "name": display}]` on the first endpoint that yields
    a non-empty, well-shaped result; empty list otherwise.
    """
    try:
        info = http_auth("GET", "/api/v3/users/info")
    except SystemExit:
        return []

    # Collect org IDs from user info
    org_ids: list[str] = []
    for org in info.get("organizations") or []:
        oid = org.get("id") if isinstance(org, dict) else None
        if oid:
            org_ids.append(oid)
    primary_org = info.get("organization") or {}
    if isinstance(primary_org, dict) and primary_org.get("id"):
        org_ids.append(primary_org["id"])
    # De-dup while preserving order
    seen_org: set[str] = set()
    org_ids = [x for x in org_ids if not (x in seen_org or seen_org.add(x))]

    candidate_paths: list[str] = []
    for oid in org_ids:
        candidate_paths += [
            f"/api/v3/organizations/{oid}/apps/",
            f"/api/v3/organization/{oid}/apps/",
        ]
    candidate_paths += ["/api/v3/apps/", "/api/v3/users/apps/"]

    def normalize(payload: Any) -> list[dict[str, str]]:
        items = payload if isinstance(payload, list) else (payload.get("apps") if isinstance(payload, dict) else None)
        if not isinstance(items, list):
            return []
        out: list[dict[str, str]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            uuid = it.get("id") or it.get("appID") or it.get("uuid")
            name = it.get("name") or it.get("displayName") or it.get("title") or "app"
            if uuid:
                out.append({"id": str(uuid), "name": str(name)})
        return out

    for path in candidate_paths:
        try:
            payload = http_auth("GET", path)
        except SystemExit:
            continue
        found = normalize(payload)
        if found:
            return found
    return []


# ---------- HTTP ----------

def http(method: str, path: str, body: Any = None, *, token: str | None = None) -> Any:
    url = f"{API_BASE}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


class AuthError(Exception):
    pass


def http_auth(method: str, path: str, body: Any = None) -> Any:
    """HTTP with auto-refresh on 401 (once)."""
    token = get_token()
    try:
        return http(method, path, body, token=token)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            body_text = e.read().decode(errors="replace")
            sys.exit(f"HTTP {e.code} on {method} {path}\n{body_text}")
    # Refresh and retry once.
    token = get_token(force_refresh=True)
    try:
        return http(method, path, body, token=token)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        sys.exit(f"HTTP {e.code} on {method} {path} (after refresh)\n{body_text}")


# ---------- Result shaping & formatting ----------

def _flatten_result(result: Any) -> tuple[list[str], list[dict]]:
    """Normalize a TelemetryDeck response to (columns, rows).

    Handles three common shapes:
      topN      — [{"timestamp":..., "result":[{dim:v, metric:n}, ...]}]
      timeseries— [{"timestamp":..., "result":{metric:n}}, ...]
      groupBy   — [{"timestamp":..., "event":{dim:v, metric:n}}, ...]

    Also unwraps the v3 envelope: {"result": {"rows": [...], "type": "..."}}
    """
    # Unwrap v3 envelope: {"result": {"rows": [...], "type": "..."}}
    if isinstance(result, dict) and isinstance(result.get("result"), dict):
        rows_val = result["result"].get("rows")
        if isinstance(rows_val, list):
            result = rows_val

    if not isinstance(result, list) or not result:
        return ([], [])
    first = result[0]
    if not isinstance(first, dict):
        return ([], [])

    inner = first.get("result")
    if isinstance(inner, list):  # topN
        rows = inner
        if not rows:
            return ([], [])
        cols: list[str] = []
        for r in rows:
            for k in r.keys():
                if k not in cols:
                    cols.append(k)
        return (cols, rows)

    if isinstance(inner, dict):  # timeseries
        cols = ["timestamp"] + list(inner.keys())
        rows = [{"timestamp": r.get("timestamp"), **r.get("result", {})} for r in result]
        return (cols, rows)

    if isinstance(first.get("event"), dict):  # groupBy
        cols = ["timestamp"]
        for r in result:
            for k in r.get("event", {}).keys():
                if k not in cols:
                    cols.append(k)
        rows = [{"timestamp": r.get("timestamp"), **r.get("event", {})} for r in result]
        return (cols, rows)

    # Unknown shape — surface keys of the first element.
    cols = list(first.keys())
    return (cols, result)


def _truncate(val: Any, cap: int = 40) -> str:
    s = "" if val is None else str(val)
    return s if len(s) <= cap else s[: cap - 1] + "…"


def format_table(cols: list[str], rows: list[dict]) -> str:
    if not rows:
        return ""
    widths: dict[str, int] = {}
    for c in cols:
        widths[c] = max(len(c), max((len(_truncate(r.get(c))) for r in rows), default=0))
    header = "| " + " | ".join(c.ljust(widths[c]) for c in cols) + " |"
    sep = "|-" + "-|-".join("-" * widths[c] for c in cols) + "-|"
    lines = [header, sep]
    for r in rows:
        lines.append("| " + " | ".join(_truncate(r.get(c)).ljust(widths[c]) for c in cols) + " |")
    return "\n".join(lines)


def format_csv(cols: list[str], rows: list[dict]) -> str:
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow(["" if r.get(c) is None else r.get(c) for c in cols])
    return buf.getvalue().rstrip("\n")


EMPTY_HINT = (
    "(no rows)\n"
    "Hints: widen --days, check the event/dimension name, confirm the app has "
    "opted-in users, or try --include-test-mode."
)


def format_result(result: Any, fmt: str) -> str:
    """Render a TelemetryDeck result for human / markdown consumption.

    Formats: `table` (markdown, default), `csv`, `json`.
    Falls back to JSON if the shape isn't recognized.
    """
    if fmt == "json":
        return json.dumps(result, indent=2)
    cols, rows = _flatten_result(result)
    if not cols:
        return EMPTY_HINT if fmt != "json" else json.dumps(result, indent=2)
    if not rows:
        return EMPTY_HINT
    if fmt == "csv":
        return format_csv(cols, rows)
    return format_table(cols, rows)


# ---------- Period comparison formatting ----------

def _is_timeseries_shape(result: Any) -> bool:
    return (
        isinstance(result, list)
        and len(result) > 0
        and isinstance(result[0], dict)
        and isinstance(result[0].get("result"), dict)
    )


def _pct(cur: float, prior: float) -> str:
    if not prior:
        return "—" if not cur else "+∞"
    return f"{(cur - prior) / prior * 100:+.1f}%"


def _timeseries_totals(result: Any) -> dict[str, float]:
    totals: dict[str, float] = {}
    for bucket in result or []:
        for k, v in (bucket.get("result") or {}).items():
            totals[k] = totals.get(k, 0) + (v or 0)
    return totals


def format_compared(primary: Any, prior: Any, fmt: str) -> str:
    """Render primary + prior-period results side by side with delta and pct.

    - Timeseries: sums each series to a scalar, emits one row per metric.
    - topN / groupBy (single bucket): joins on the dimension column(s), emits
      current | prior | delta | pct per row.
    - Unknown shape: falls back to two labeled blocks.
    """
    if fmt == "json":
        return json.dumps({"current": primary, "prior": prior}, indent=2)

    # Timeseries → totals table
    if _is_timeseries_shape(primary):
        t_cur = _timeseries_totals(primary)
        t_pri = _timeseries_totals(prior)
        rows = []
        for k in t_cur or t_pri:
            cur = t_cur.get(k, 0)
            pri = t_pri.get(k, 0)
            rows.append({
                "metric": k,
                "current": cur,
                "prior": pri,
                "delta": cur - pri,
                "pct": _pct(cur, pri),
            })
        cols = ["metric", "current", "prior", "delta", "pct"]
        body = format_table(cols, rows) if fmt == "table" else format_csv(cols, rows)
        return body

    # topN / groupBy → merge on dimension
    pcols, prows = _flatten_result(primary)
    _, qrows = _flatten_result(prior)
    metric_col = next((c for c in ("count", "users") if c in pcols), None)
    if metric_col is None or not prows:
        sep = "\n\n--- PRIOR PERIOD ---\n\n"
        return format_result(primary, fmt) + sep + format_result(prior, fmt)

    dim_cols = [c for c in pcols if c not in (metric_col, "timestamp")]

    def key(row: dict) -> tuple:
        return tuple(row.get(c) for c in dim_cols)

    prior_map = {key(r): (r.get(metric_col) or 0) for r in qrows}
    seen: set[tuple] = set()
    rows: list[dict] = []
    for r in prows:
        k = key(r)
        seen.add(k)
        cur = r.get(metric_col) or 0
        pri = prior_map.get(k, 0)
        row = {c: r.get(c) for c in dim_cols}
        row.update({
            f"{metric_col}_current": cur,
            f"{metric_col}_prior": pri,
            "delta": cur - pri,
            "pct": _pct(cur, pri),
        })
        rows.append(row)
    # Append prior-only rows (dims that vanished)
    for r in qrows:
        k = key(r)
        if k in seen:
            continue
        pri = r.get(metric_col) or 0
        row = {c: r.get(c) for c in dim_cols}
        row.update({
            f"{metric_col}_current": 0,
            f"{metric_col}_prior": pri,
            "delta": -pri,
            "pct": "—",
        })
        rows.append(row)
    cols = dim_cols + [f"{metric_col}_current", f"{metric_col}_prior", "delta", "pct"]
    return format_table(cols, rows) if fmt == "table" else format_csv(cols, rows)


# ---------- Query orchestration ----------

def inject_auto_filter(query: dict, app_id: str, include_test_mode: bool) -> dict:
    """Replace the sentinel filter with the mandatory appID + isTestMode filter.

    Also ensures top-level queries without any filter still scope to this app.
    """
    filt = query.get("filter")
    needs_wrap = (
        filt is None
        or (isinstance(filt, dict) and filt.get(AUTO_FILTER_SENTINEL) is True)
    )
    if needs_wrap:
        query["filter"] = app_filter(app_id, include_test_mode=include_test_mode)
    return query


def app_filter(app_id: str, *, include_test_mode: bool = False) -> dict:
    fields: list[dict] = [{"type": "selector", "dimension": "appID", "value": app_id}]
    if not include_test_mode:
        fields.append({"type": "selector", "dimension": "isTestMode", "value": "false"})
    return {"type": "and", "fields": fields}


def run_query(query: dict, *, poll_interval: float = 1.0, timeout_s: float = 120.0) -> Any:
    task = http_auth("POST", "/api/v3/query/calculate-async/", query)
    task_id = task.get("queryTaskID") or task.get("id") or task.get("taskID")
    if not task_id:
        sys.exit(f"No task id in response: {task}")
    deadline = time.time() + timeout_s
    polls = 0
    while True:
        status = http_auth("GET", f"/api/v3/task/{task_id}/status/")
        state = status.get("status") or status.get("state")
        if state == "successful":
            break
        if state == "failed":
            sys.exit(f"Query failed: {status}")
        if time.time() > deadline:
            sys.exit(f"Query timed out after {timeout_s}s (last state: {state})")
        polls += 1
        if polls > 30:
            sys.exit(f"Query exceeded 30 polls — reshape the query (state: {state})")
        time.sleep(poll_interval)
    raw = http_auth("GET", f"/api/v3/task/{task_id}/value/")
    if os.environ.get("TDQ_RAW"):
        sys.stderr.write("--- RAW QUERY RESULT ---\n")
        sys.stderr.write(json.dumps(raw, indent=2) + "\n")
        sys.stderr.write("------------------------\n")
    # Unwrap v3 envelope once, at the source. Downstream code expects the
    # list-of-buckets shape. See `_flatten_result` for the shape guide.
    return _unwrap_envelope(raw)


def _unwrap_envelope(result: Any) -> Any:
    """Strip the v3 `{"result": {"rows": [...], "type": "..."}}` envelope.

    Pass-through for already-unwrapped list results. Returns [] for an empty
    envelope rather than the dict, so downstream `result[0]` works uniformly.
    """
    if isinstance(result, dict) and isinstance(result.get("result"), dict):
        rows_val = result["result"].get("rows")
        if isinstance(rows_val, list):
            return rows_val
    return result


def relative_interval(days: int) -> dict:
    return {
        "beginningDate": {"component": "day", "offset": -days, "position": "beginning"},
        "endDate": {"component": "day", "offset": 0, "position": "end"},
    }


def month_interval(months: int) -> dict:
    return {
        "beginningDate": {"component": "month", "offset": -months, "position": "beginning"},
        "endDate": {"component": "month", "offset": 0, "position": "end"},
    }


# ---------- Named intervals ----------

_NAMED_INTERVALS: dict[str, tuple[dict, dict]] = {
    "last-week":  ({"component":"week",  "offset":-1,"position":"beginning"},
                   {"component":"week",  "offset":-1,"position":"end"}),
    "this-week":  ({"component":"week",  "offset": 0,"position":"beginning"},
                   {"component":"day",   "offset": 0,"position":"end"}),
    "last-month": ({"component":"month", "offset":-1,"position":"beginning"},
                   {"component":"month", "offset":-1,"position":"end"}),
    "this-month": ({"component":"month", "offset": 0,"position":"beginning"},
                   {"component":"day",   "offset": 0,"position":"end"}),
    "last-year":  ({"component":"year",  "offset":-1,"position":"beginning"},
                   {"component":"year",  "offset":-1,"position":"end"}),
    "this-year":  ({"component":"year",  "offset": 0,"position":"beginning"},
                   {"component":"day",   "offset": 0,"position":"end"}),
}

_INTERVAL_ALIASES = {
    "wtd": "this-week",
    "mtd": "this-month",
    "ytd": "this-year",
}


def parse_interval(spec: str) -> dict:
    """Parse a named or `last-Nd` interval spec into a TQL relativeInterval.

    Accepts: `last-Nd` (any positive integer N), plus named windows
    (`last-week`, `this-week`, `last-month`, `this-month`, `last-year`,
    `this-year`) and their common aliases (`wtd`, `mtd`, `ytd`).
    """
    import re
    spec = spec.strip().lower()
    spec = _INTERVAL_ALIASES.get(spec, spec)
    m = re.match(r"^last-(\d+)d$", spec)
    if m:
        return relative_interval(int(m.group(1)))
    if spec in _NAMED_INTERVALS:
        b, e = _NAMED_INTERVALS[spec]
        return {"beginningDate": dict(b), "endDate": dict(e)}
    raise ValueError(
        f"Unknown interval spec: {spec!r}. "
        f"Use `last-Nd` or one of: {', '.join(sorted(_NAMED_INTERVALS))} "
        f"(aliases: {', '.join(sorted(_INTERVAL_ALIASES))})."
    )


def prior_period(interval: dict) -> dict:
    """Shift a relativeInterval back by one period of the same shape.

    Handles three shapes:
      - `last-Nd`  (both boundaries on `day`) → shift by N days.
      - Complete prior period (both boundaries same component, same offset,
        e.g. `last-month`) → decrement offset.
      - `this-X` to-date (begin on X at offset 0, end on day 0) → take the
        complete prior X period (e.g. prior of `this-month` is `last-month`).
    """
    b = interval["beginningDate"]
    e = interval["endDate"]
    bc, bo = b["component"], b["offset"]
    ec, eo = e["component"], e["offset"]

    # Case 1: trailing days (last-Nd)
    if bc == "day" and ec == "day":
        length = abs(bo - eo) or 1
        return {
            "beginningDate": {"component": "day", "offset": bo - length, "position": "beginning"},
            "endDate":       {"component": "day", "offset": eo - length, "position": "end"},
        }

    # Case 2: complete period (last-week, last-month, last-year) — both boundaries identical
    if bc == ec and bo == eo:
        return {
            "beginningDate": {**b, "offset": bo - 1},
            "endDate":       {**e, "offset": eo - 1},
        }

    # Case 3: this-X to-date → take the complete prior X
    if bo == 0 and ec == "day" and eo == 0:
        return {
            "beginningDate": {"component": bc, "offset": -1, "position": "beginning"},
            "endDate":       {"component": bc, "offset": -1, "position": "end"},
        }

    raise ValueError(
        f"Cannot compute prior-period for interval shape: {interval}. "
        "Supported: last-Nd, complete periods (last-week/month/year), "
        "and to-date periods (this-week/month/year)."
    )


def resolve_interval(days: int | None, interval_spec: str | None) -> dict:
    """Pick the interval. `--interval` wins; else fall back to `--days`."""
    if interval_spec:
        return parse_interval(interval_spec)
    if days is not None:
        return relative_interval(days)
    return relative_interval(30)


def add_event_selector(filt: dict, event: str | None) -> dict:
    """Append a `type=<event>` selector to an existing `and`-filter if event is set."""
    if event:
        filt["fields"].append({"type": "selector", "dimension": "type", "value": event})
    return filt


# ---------- Subcommands ----------

def cmd_login(args: argparse.Namespace) -> None:
    """Interactive setup: email + password, then pick an app from the user's list.

    After the bearer is minted, the CLI calls the TelemetryDeck API to list the
    user's apps and offers a numbered selector. Falls back to manual UUID entry
    if the API listing fails. An explicit `--app-id` bypasses the selector and
    is saved as the current app directly.
    """
    cfg = load_config()
    if args.reset:
        secret_delete(SECRET_ACCT_PASSWORD)
        secret_delete(SECRET_ACCT_TOKEN)
        cfg.pop("email", None)
        cfg.pop("token_expires_at", None)
        save_config(cfg)
        cfg = load_config()
    token = _store_pat(cfg, args.pat) if args.pat is not None else _mint_token(cfg, interactive=True)
    info = http("GET", "/api/v3/users/info", token=token)
    cfg = load_config()  # the step above persisted email (and any expiry)

    # App selection
    chosen: str | None = args.app_id
    if not chosen:
        print()
        print("Discovering apps for your account…")
        found = discover_apps()
        if found:
            ordered = sorted(found, key=lambda a: (a["name"].lower(), a["id"]))
            print("Apps available:")
            for i, app in enumerate(ordered, 1):
                print(f"  [{i}] {app['name']}  ({app['id']})")
            # Register all discovered apps so `apps` lists them without a second round-trip.
            for app in ordered:
                register_app(cfg, app["id"], app["name"])
            if not sys.stdin.isatty():
                if len(ordered) == 1:
                    chosen = ordered[0]["id"]
                    print(f"Non-interactive: auto-selected only app: {ordered[0]['name']}  ({chosen})")
                else:
                    uuids = "\n".join(f"  tdq login --app-id {a['id']}   # {a['name']}" for a in ordered)
                    print(
                        f"stdin is not a TTY — cannot prompt for app selection.\n"
                        f"Re-run with --app-id in your terminal:\n{uuids}",
                        file=sys.stderr,
                    )
                    sys.exit(0)
            else:
                pick = input("Pick an app (number, or paste a UUID, or blank to skip): ").strip()
                if pick:
                    if pick.isdigit() and 1 <= int(pick) <= len(ordered):
                        chosen = ordered[int(pick) - 1]["id"]
                    else:
                        chosen = pick
        else:
            if not sys.stdin.isatty():
                sys.exit(
                    "Could not auto-list apps and stdin is not a TTY.\n"
                    "Run `tdq login --app-id <UUID>` directly in your terminal."
                )
            print(
                "Could not auto-list apps (endpoint not exposed or no access). "
                "Paste an app UUID below, or press Enter to configure later with "
                "`tdq apps add <uuid>`."
            )
            manual = input("App UUID (optional): ").strip()
            if manual:
                chosen = manual
    if chosen:
        set_current_app(cfg, chosen)
    save_config(cfg)

    summary: dict[str, Any] = {
        "user_id": info.get("id") or info.get("user", {}).get("id"),
        "email": cfg.get("email"),
        "current_app_id": cfg.get("current_app_id"),
        "registered_apps": len(_apps_dict(cfg)),
        "secret_backend": secret_backend(),
    }
    print()
    print("login ok:", json.dumps(summary, indent=2))


def cmd_logout(args: argparse.Namespace) -> None:
    """Wipe the stored bearer, password, config, and any file-backend secrets."""
    secret_delete(SECRET_ACCT_PASSWORD)
    secret_delete(SECRET_ACCT_TOKEN)
    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()
    if SECRET_FILE_PATH.exists():
        try:
            SECRET_FILE_PATH.unlink()
        except OSError:
            pass
    print(f"logged out: secrets cleared ({secret_backend()} backend), config file removed")


def cmd_whoami(args: argparse.Namespace) -> None:
    info = http_auth("GET", "/api/v3/users/info")
    print(json.dumps(info, indent=2))


# ---------- App management ----------

def _print_app_table(cfg: dict[str, Any]) -> None:
    """Print the registered-apps table with a `*` marker on the current app."""
    apps = _apps_dict(cfg)
    if not apps:
        print("(no apps registered — run `tdq.py login` or `tdq.py apps add <uuid>`)")
        return
    current = cfg.get("current_app_id")
    ordered = sorted(apps.items(), key=lambda kv: ((kv[1] or "").lower(), kv[0]))
    # Column widths
    idx_w = len(str(len(ordered)))
    name_w = max((len(n or "") for _, n in ordered), default=4)
    print(f"{'':>{idx_w}}    {'name':<{name_w}}  app_id")
    for i, (uuid, name) in enumerate(ordered, 1):
        mark = "*" if uuid == current else " "
        print(f"{i:>{idx_w}}  {mark} {(name or ''):<{name_w}}  {uuid}")
    if current:
        cur_name = apps.get(current) or ""
        print(f"\ncurrent: {cur_name}  ({current})")


def cmd_apps_list(args: argparse.Namespace) -> None:
    """List registered apps with the current one marked `*`."""
    cfg = load_config()
    _print_app_table(cfg)


def cmd_apps_use(args: argparse.Namespace) -> None:
    """Switch the current app. `selector` is a UUID, a name, or the index from `apps`."""
    cfg = load_config()
    resolved = resolve_app_selector(cfg, args.selector)
    if not resolved:
        # Allow switching to an unregistered UUID directly — register on the fly.
        if len(args.selector) >= 8 and args.selector.count("-") in (0, 4):
            resolved = args.selector
            register_app(cfg, resolved, args.name)
        else:
            sys.exit(
                f"No match for {args.selector!r}. Try `tdq.py apps` to see registered apps."
            )
    set_current_app(cfg, resolved)
    if args.name:
        _apps_dict(cfg)[resolved] = args.name
    save_config(cfg)
    name = _apps_dict(cfg).get(resolved) or ""
    print(f"current app → {name}  ({resolved})")


def cmd_apps_add(args: argparse.Namespace) -> None:
    """Register an additional app by UUID (+ optional display name)."""
    cfg = load_config()
    register_app(cfg, args.app_id, args.name)
    if args.set_current or not cfg.get("current_app_id"):
        set_current_app(cfg, args.app_id)
    save_config(cfg)
    _print_app_table(cfg)


def cmd_apps_remove(args: argparse.Namespace) -> None:
    """Unregister an app (by UUID, name, or index from `apps`)."""
    cfg = load_config()
    resolved = resolve_app_selector(cfg, args.selector)
    if not resolved:
        sys.exit(f"No match for {args.selector!r}.")
    apps = _apps_dict(cfg)
    apps.pop(resolved, None)
    if cfg.get("current_app_id") == resolved:
        cfg.pop("current_app_id", None)
        # If anything else remains, promote arbitrarily so the user isn't stuck.
        if apps:
            cfg["current_app_id"] = next(iter(sorted(apps)))
    save_config(cfg)
    _print_app_table(cfg)


def cmd_apps_refresh(args: argparse.Namespace) -> None:
    """Re-fetch the app list from the API and merge into the config."""
    cfg = load_config()
    found = discover_apps()
    if not found:
        sys.exit(
            "API returned no apps. The listing endpoint may not be exposed for "
            "your account yet; add manually with `tdq.py apps add <uuid>`."
        )
    for app in found:
        register_app(cfg, app["id"], app["name"])
    if not cfg.get("current_app_id"):
        set_current_app(cfg, found[0]["id"])
    save_config(cfg)
    _print_app_table(cfg)


def cmd_doctor(args: argparse.Namespace) -> None:
    """End-to-end setup check: platform, config, keychain, auth, query round-trip."""
    any_fail = False

    def check(name: str, fn) -> None:
        nonlocal any_fail
        print(f"  [?] {name}...", end=" ", flush=True)
        try:
            msg = fn()
            print(f"OK{(' ' + msg) if msg else ''}")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL: {e}")
            any_fail = True

    print("TelemetryDeck CLI — doctor")

    def c_platform():
        backend = secret_backend()
        platform_label = sys.platform
        hint = ""
        if backend == "file":
            hint = (
                " — file-backed store (no OS keychain on this platform; "
                "install libsecret's `secret-tool` on Linux for stronger protection)"
            )
        return f"({platform_label}, secret backend: {backend}{hint})"

    def c_config():
        cfg = load_config()
        if not cfg.get("email"):
            raise RuntimeError("no email on file — run `tdq.py login`")
        apps = _apps_dict(cfg)
        n = len(apps)
        current = cfg.get("current_app_id") or "(none)"
        return f"(email={cfg.get('email')}, apps={n}, current={current[:8]}…)"

    def c_keychain():
        if secret_get(SECRET_ACCT_TOKEN) is None:
            if secret_get(SECRET_ACCT_PASSWORD) is None:
                raise RuntimeError("no token and no password — run `tdq.py login`")
            return "(no cached token, will re-mint from stored password)"
        return ""

    def c_whoami():
        info = http_auth("GET", "/api/v3/users/info")
        uid = info.get("id") or info.get("user", {}).get("id") or "?"
        return f"(user_id={uid})"

    def c_query():
        app_id = (
            load_config().get("current_app_id")
            or os.environ.get("TELEMETRYDECK_APP_ID")
        )
        if not app_id:
            raise RuntimeError("no current app — run `tdq.py login` or `tdq.py apps use <uuid>`")
        # Widen to 7d — 1d can legitimately be empty on a quiet app and would
        # mask a real parse bug. 7d should have something for any live app.
        q = {
            "queryType": "topN",
            "dataSource": "telemetry-signals",
            "granularity": "all",
            "aggregations": [{"type": "eventCount", "name": "count"}],
            "metric": {"type": "numeric", "metric": "count"},
            "dimension": {"type": "default", "dimension": "type", "outputName": "event"},
            "filter": app_filter(app_id),
            "relativeIntervals": [relative_interval(7)],
            "threshold": 5,
        }
        result = run_query(q)
        _, rows = _flatten_result(result)
        if not rows:
            raise RuntimeError(
                "query succeeded but returned 0 rows — either (a) no signals "
                "in the last 7d (check `tdq.py signals --days 30`), (b) wrong "
                "appID, or (c) a result-shape regression. Re-run with env "
                "TDQ_RAW=1 to see the raw response."
            )
        return f"(7-day topN for app {app_id[:8]}…, {len(rows)} events)"

    check("platform", c_platform)
    check("config file", c_config)
    check("secret store", c_keychain)
    check("whoami (auth)", c_whoami)
    check("query round-trip", c_query)

    sys.exit(1 if any_fail else 0)


def cmd_insights(args: argparse.Namespace) -> None:
    app_id = get_app_id(args, interactive=True)
    # The v3 API exposes app detail via a few nearby endpoints; the insight
    # group tree hangs off the app resource. Try each until one responds.
    last_err: str | None = None
    for path in (
        f"/api/v3/apps/{app_id}/insightgroups/",
        f"/api/v3/apps/{app_id}/insights/",
        f"/api/v3/apps/{app_id}/",
    ):
        try:
            res = http_auth("GET", path)
            print(json.dumps(res, indent=2))
            return
        except SystemExit as e:
            last_err = str(e)
    sys.exit(f"All insight endpoints failed. Last: {last_err}")


def cmd_insight(args: argparse.Namespace) -> None:
    payload = {"relativeInterval": relative_interval(args.days)}
    resolved = http_auth("POST", f"/api/v3/insights/{args.id}/query/", payload)
    if args.resolve_only:
        print(json.dumps(resolved, indent=2))
        return
    result = run_query(resolved)
    _emit_raw_if_requested(result, args)
    print(format_result(result, args.format))


def cmd_query(args: argparse.Namespace) -> None:
    raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
    query = json.loads(raw)
    app_id = get_app_id(args, interactive=True)
    inject_auto_filter(query, app_id, include_test_mode=args.include_test_mode)
    result = run_query(query)
    _emit_raw_if_requested(result, args)
    print(format_result(result, args.format))


def cmd_signals(args: argparse.Namespace) -> None:
    app_id = get_app_id(args, interactive=True)
    query = {
        "queryType": "topN",
        "dataSource": "telemetry-signals",
        "granularity": "all",
        "aggregations": [{"type": "eventCount", "name": "count"}],
        "metric": {"type": "numeric", "metric": "count"},
        "dimension": {"type": "default", "dimension": "type", "outputName": "event"},
        "filter": app_filter(app_id, include_test_mode=args.include_test_mode),
        "relativeIntervals": [relative_interval(args.days)],
        "threshold": args.top,
    }
    result = run_query(query)
    _emit_raw_if_requested(result, args)
    print(format_result(result, args.format))


# ---- Recipes ----

def _run_with_compare(query: dict, args: argparse.Namespace) -> str:
    """Run a query and optionally compare against prior-period. Honors --raw."""
    compare = getattr(args, "compare", None)
    fmt = args.format
    primary = run_query(query)
    _emit_raw_if_requested(primary, args)
    if not compare:
        return format_result(primary, fmt)
    if compare != "prior-period":
        sys.exit(f"Unsupported --compare mode: {compare}. Only 'prior-period' is supported.")
    prior_query = json.loads(json.dumps(query))  # deep copy without importing copy
    prior_query["relativeIntervals"] = [prior_period(query["relativeIntervals"][0])]
    prior = run_query(prior_query)
    _emit_raw_if_requested(prior, args)
    return format_compared(primary, prior, fmt)


def cmd_dau(args: argparse.Namespace) -> None:
    """Daily active users. `--event` narrows to one signal."""
    app_id = get_app_id(args, interactive=True)
    filt = add_event_selector(
        app_filter(app_id, include_test_mode=args.include_test_mode), args.event
    )
    query = {
        "queryType": "timeseries",
        "dataSource": "telemetry-signals",
        "granularity": "day",
        "aggregations": [
            {"type": "thetaSketch", "name": "users", "fieldName": "clientUser"}
        ],
        "filter": filt,
        "relativeIntervals": [resolve_interval(args.days, args.interval)],
    }
    print(_run_with_compare(query, args))


def cmd_mau(args: argparse.Namespace) -> None:
    """Monthly active users."""
    app_id = get_app_id(args, interactive=True)
    filt = add_event_selector(
        app_filter(app_id, include_test_mode=args.include_test_mode), args.event
    )
    # MAU: prefer `--interval` if given, otherwise fall back to the legacy --months flag.
    if args.interval:
        interval = parse_interval(args.interval)
    else:
        interval = month_interval(args.months)
    query = {
        "queryType": "timeseries",
        "dataSource": "telemetry-signals",
        "granularity": "month",
        "aggregations": [
            {"type": "thetaSketch", "name": "users", "fieldName": "clientUser"}
        ],
        "filter": filt,
        "relativeIntervals": [interval],
    }
    print(_run_with_compare(query, args))


def cmd_groupby(args: argparse.Namespace) -> None:
    """Break a metric down by an arbitrary dimension.

    Example: `tdq.py groupby license --event App_launched --interval last-30d`.
    Example: `tdq.py groupby majorSystemVersion --interval this-month --metric users --compare prior-period`.
    """
    app_id = get_app_id(args, interactive=True)
    filt = add_event_selector(
        app_filter(app_id, include_test_mode=args.include_test_mode), args.event
    )
    if args.metric == "users":
        agg = {"type": "thetaSketch", "name": "users", "fieldName": "clientUser"}
    else:
        agg = {"type": "eventCount", "name": "count"}
    query = {
        "queryType": "groupBy",
        "dataSource": "telemetry-signals",
        "granularity": "all",
        "dimensions": [
            {"type": "default", "dimension": args.dimension, "outputName": args.dimension}
        ],
        "aggregations": [agg],
        "filter": filt,
        "relativeIntervals": [resolve_interval(args.days, args.interval)],
    }
    print(_run_with_compare(query, args))


def cmd_events(args: argparse.Namespace) -> None:
    """Merged 7-day + 30-day event counts — schema discovery.

    Runs two `topN` queries and joins them so you can see at a glance which
    events still fire (7d) vs. which ever fired in the last month (30d). Use
    this before writing a recipe with `--event E` to confirm E exists and is
    spelled correctly.
    """
    app_id = get_app_id(args, interactive=True)

    def run(days: int) -> list:
        q = {
            "queryType": "topN",
            "dataSource": "telemetry-signals",
            "granularity": "all",
            "aggregations": [{"type": "eventCount", "name": "count"}],
            "metric": {"type": "numeric", "metric": "count"},
            "dimension": {"type": "default", "dimension": "type", "outputName": "event"},
            "filter": app_filter(app_id, include_test_mode=args.include_test_mode),
            "relativeIntervals": [relative_interval(days)],
            "threshold": args.top,
        }
        return run_query(q)

    def _event_counts(result: Any) -> dict[str, int]:
        """Extract {event: count} regardless of envelope / bucket shape."""
        _, rows = _flatten_result(result)
        out: dict[str, int] = {}
        for r in rows:
            ev = r.get("event") or r.get("type")
            if ev is None:
                continue
            out[ev] = int(r.get("count") or 0)
        return out

    r7 = run(7)
    r30 = run(30)
    _emit_raw_if_requested({"r7": r7, "r30": r30}, args)
    c7 = _event_counts(r7)
    c30 = _event_counts(r30)
    all_events = sorted(set(c7) | set(c30), key=lambda e: c30.get(e, 0), reverse=True)
    rows = [
        {"event": e, "count_7d": c7.get(e, 0), "count_30d": c30.get(e, 0)}
        for e in all_events
    ]
    cols = ["event", "count_7d", "count_30d"]
    if args.format == "json":
        print(json.dumps(rows, indent=2))
    elif args.format == "csv":
        print(format_csv(cols, rows))
    else:
        print(format_table(cols, rows) if rows else EMPTY_HINT)


def cmd_test(args: argparse.Namespace) -> None:
    """Run 3 known-good queries (timeseries, topN, groupBy) + print raw shapes.

    One-shot sanity check for when API response shape might have changed.
    Each query is widened to 7d so results should not be empty on a live app.
    Emits the raw post-unwrap JSON for every result plus a parse summary per
    shape so regressions surface immediately.
    """
    app_id = get_app_id(args, interactive=True)
    filt = app_filter(app_id, include_test_mode=args.include_test_mode)
    interval = relative_interval(7)

    queries: dict[str, dict] = {
        "timeseries": {
            "queryType": "timeseries",
            "dataSource": "telemetry-signals",
            "granularity": "day",
            "aggregations": [{"type": "eventCount", "name": "count"}],
            "filter": filt,
            "relativeIntervals": [interval],
        },
        "topN": {
            "queryType": "topN",
            "dataSource": "telemetry-signals",
            "granularity": "all",
            "aggregations": [{"type": "eventCount", "name": "count"}],
            "metric": {"type": "numeric", "metric": "count"},
            "dimension": {"type": "default", "dimension": "type", "outputName": "event"},
            "threshold": 5,
            "filter": filt,
            "relativeIntervals": [interval],
        },
        "groupBy": {
            "queryType": "groupBy",
            "dataSource": "telemetry-signals",
            "granularity": "all",
            "dimensions": [{"type": "default", "dimension": "type", "outputName": "event"}],
            "aggregations": [{"type": "eventCount", "name": "count"}],
            "filter": filt,
            "relativeIntervals": [interval],
        },
    }

    any_fail = False
    for name, q in queries.items():
        print(f"=== {name} ===")
        try:
            result = run_query(q)
        except SystemExit as e:
            print(f"  FAIL: run_query raised: {e}")
            any_fail = True
            continue
        print("  raw (post-unwrap):")
        print("  " + json.dumps(result, indent=2).replace("\n", "\n  "))
        cols, rows = _flatten_result(result)
        print(f"  parsed → cols={cols} rows={len(rows)}")
        if not rows:
            print("  WARN: zero rows parsed. Either no signals in window, or shape regression.")
            any_fail = True
        print()
    sys.exit(1 if any_fail else 0)


# ---------- Argparse wiring ----------

def _add_app_id(p: argparse.ArgumentParser) -> None:
    p.add_argument("--app-id", help="TelemetryDeck app UUID (overrides config/env).")


def _add_test_mode(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--include-test-mode",
        action="store_true",
        help="Include isTestMode=true signals (default: exclude).",
    )


def _add_format(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--format",
        choices=("table", "csv", "json"),
        default="table",
        help="Output format (default: table — markdown-ready).",
    )
    p.add_argument(
        "--raw",
        action="store_true",
        help="Print the raw TelemetryDeck response to stderr before formatting. "
             "Use when the formatted output is empty to see if the result is "
             "genuinely empty or the parser dropped it.",
    )


def _emit_raw_if_requested(result: Any, args: argparse.Namespace) -> None:
    """Print raw JSON to stderr if --raw was set or TDQ_RAW env var is truthy."""
    if getattr(args, "raw", False) or os.environ.get("TDQ_RAW"):
        sys.stderr.write("--- POST-UNWRAP RESULT ---\n")
        sys.stderr.write(json.dumps(result, indent=2) + "\n")
        sys.stderr.write("--------------------------\n")


def _add_interval(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--interval",
        help=(
            "Named interval: last-Nd, last-week, this-week, last-month, this-month, "
            "last-year, this-year (aliases: wtd, mtd, ytd). Overrides --days."
        ),
    )


def _add_compare(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--compare",
        choices=("prior-period",),
        help="Run the same query against the immediately prior period and show delta + pct.",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tdq",
        description="TelemetryDeck v3 query CLI (Keychain-backed auth).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser(
        "login",
        help="Prompt for email/password (or use --pat), mint a bearer, then pick an app from your account.",
    )
    p_login.add_argument(
        "--app-id",
        help="Skip the interactive app picker and use this UUID directly.",
    )
    p_login.add_argument(
        "--pat",
        nargs="?",
        const="",
        metavar="TOKEN",
        help="Authenticate with a personal access token instead of email/password — "
             "the only option for SSO accounts, which have no password. Pass the "
             "token, omit it to be prompted, or pipe it on stdin.",
    )
    p_login.add_argument("--reset", action="store_true", help="Wipe existing creds first.")
    p_login.set_defaults(func=cmd_login)

    sub.add_parser("logout", help="Clear stored secrets and config file.").set_defaults(func=cmd_logout)

    sub.add_parser("whoami", help="Verify token; show user/org info.").set_defaults(func=cmd_whoami)
    sub.add_parser("doctor", help="End-to-end setup check with remediation hints.").set_defaults(func=cmd_doctor)

    # ---- App management ----
    p_apps = sub.add_parser(
        "apps",
        help="List / add / remove / switch the app the CLI queries against.",
    )
    apps_sub = p_apps.add_subparsers(dest="apps_cmd")
    p_apps.set_defaults(func=cmd_apps_list)  # bare `tdq.py apps` → list

    p_apps_list = apps_sub.add_parser("list", help="List registered apps (default if no subcommand).")
    p_apps_list.set_defaults(func=cmd_apps_list)

    p_apps_use = apps_sub.add_parser("use", help="Switch the current app.")
    p_apps_use.add_argument("selector", help="UUID, display name, or 1-based index from `apps`.")
    p_apps_use.add_argument("--name", help="Set/override the display name for this app.")
    p_apps_use.set_defaults(func=cmd_apps_use)

    p_apps_add = apps_sub.add_parser("add", help="Register an additional app by UUID.")
    p_apps_add.add_argument("app_id", help="App UUID from the TelemetryDeck dashboard.")
    p_apps_add.add_argument("--name", help="Display name (defaults to 'app').")
    p_apps_add.add_argument(
        "--set-current", action="store_true",
        help="Also switch to this app as the current one.",
    )
    p_apps_add.set_defaults(func=cmd_apps_add)

    p_apps_remove = apps_sub.add_parser("remove", help="Unregister an app.")
    p_apps_remove.add_argument("selector", help="UUID, display name, or index.")
    p_apps_remove.set_defaults(func=cmd_apps_remove)

    p_apps_refresh = apps_sub.add_parser(
        "refresh",
        help="Re-fetch the app list from the TelemetryDeck API and merge into config.",
    )
    p_apps_refresh.set_defaults(func=cmd_apps_refresh)

    p_test = sub.add_parser(
        "test",
        help="Run 3 known-good queries and print raw shapes — sanity check for API shape changes.",
    )
    _add_app_id(p_test)
    _add_test_mode(p_test)
    p_test.set_defaults(func=cmd_test)

    p_insights = sub.add_parser("insights", help="List saved insights for the app.")
    _add_app_id(p_insights)
    p_insights.set_defaults(func=cmd_insights)

    p_insight = sub.add_parser("insight", help="Run a saved insight by ID.")
    p_insight.add_argument("id")
    p_insight.add_argument("--days", type=int, default=30)
    p_insight.add_argument("--resolve-only", action="store_true",
                           help="Print resolved TQL; don't execute.")
    _add_format(p_insight)
    p_insight.set_defaults(func=cmd_insight)

    p_query = sub.add_parser("query", help="Run raw TQL from file or stdin.")
    p_query.add_argument("file", help="Path to JSON TQL file, or - for stdin.")
    _add_app_id(p_query)
    _add_test_mode(p_query)
    _add_format(p_query)
    p_query.set_defaults(func=cmd_query)

    p_signals = sub.add_parser("signals", help="Top events over last N days (topN on dimension=type).")
    p_signals.add_argument("--days", type=int, default=30)
    p_signals.add_argument("--top", type=int, default=25)
    _add_app_id(p_signals)
    _add_test_mode(p_signals)
    _add_format(p_signals)
    p_signals.set_defaults(func=cmd_signals)

    # ---- Recipes ----
    p_dau = sub.add_parser("dau", help="Daily active users (cardinality of clientUser per day).")
    p_dau.add_argument("--days", type=int, default=30)
    p_dau.add_argument("--event", help="Only count users who fired this specific event.")
    _add_interval(p_dau)
    _add_compare(p_dau)
    _add_app_id(p_dau)
    _add_test_mode(p_dau)
    _add_format(p_dau)
    p_dau.set_defaults(func=cmd_dau)

    p_mau = sub.add_parser("mau", help="Monthly active users for the last N calendar months.")
    p_mau.add_argument("--months", type=int, default=6)
    p_mau.add_argument("--event", help="Only count users who fired this specific event.")
    _add_interval(p_mau)
    _add_compare(p_mau)
    _add_app_id(p_mau)
    _add_test_mode(p_mau)
    _add_format(p_mau)
    p_mau.set_defaults(func=cmd_mau)

    p_gb = sub.add_parser("groupby", help="Break down a metric by a dimension (license, version, etc.).")
    p_gb.add_argument("dimension", help="Dimension to group on, e.g. 'license', 'majorSystemVersion'.")
    p_gb.add_argument("--event", help="Scope to a specific event type.")
    p_gb.add_argument("--days", type=int, default=30)
    p_gb.add_argument("--metric", choices=("count", "users"), default="count",
                      help="'count' = event count; 'users' = unique clientUser cardinality.")
    _add_interval(p_gb)
    _add_compare(p_gb)
    _add_app_id(p_gb)
    _add_test_mode(p_gb)
    _add_format(p_gb)
    p_gb.set_defaults(func=cmd_groupby)

    p_events = sub.add_parser("events", help="Schema discovery: merged 7-day + 30-day event counts.")
    p_events.add_argument("--top", type=int, default=100, help="Row cap for each period (default 100).")
    _add_app_id(p_events)
    _add_test_mode(p_events)
    _add_format(p_events)
    p_events.set_defaults(func=cmd_events)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
