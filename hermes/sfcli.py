"""Thin wrapper around the Salesforce CLI.

Two environment hazards this module exists to absorb (both hit us for real while
building this):

1. Corporate TLS interception (Netskope) re-signs HTTPS traffic. Node ships its own
   CA bundle rather than reading the macOS keychain, so every `sf` network call dies
   with SELF_SIGNED_CERT_IN_CHAIN. Salesforce reports that as
   "AuthCodeExchangeError: Invalid client credentials", which sends you chasing the
   wrong problem entirely. NODE_EXTRA_CA_CERTS fixes it.

2. `sf` crashes at import on Node < 22 (`webidl.util.markAsUncloneable`). We resolve
   an explicit Node 22+ binary rather than trusting whatever PATH offers, because a
   stale nvm default silently reintroduces the crash.

Relying on the caller's shell for either of these is how this breaks in CI.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

CA_BUNDLE = Path.home() / ".certs" / "all-system-ca.pem"
MIN_NODE_MAJOR = 22


class SalesforceError(RuntimeError):
    """A CLI invocation failed. Carries the raw CLI payload for diagnosis."""

    def __init__(self, message: str, payload: dict | None = None):
        super().__init__(message)
        self.payload = payload or {}


def _resolve_sf() -> list[str]:
    """Return an argv prefix that runs `sf` under a Node new enough to survive import."""
    override = os.environ.get("HERMES_SF_BIN")
    if override:
        return [override]

    # Prefer an nvm-installed Node >= 22 driving the CLI's run.js directly. This
    # sidesteps the shebang in the `sf` shim, which points at whatever Node owned
    # the install and is exactly how the v21 crash comes back.
    nvm_versions = Path.home() / ".nvm" / "versions" / "node"
    if nvm_versions.is_dir():
        def major(p: Path) -> int:
            try:
                return int(p.name.lstrip("v").split(".")[0])
            except (ValueError, IndexError):
                return -1

        for version_dir in sorted(nvm_versions.iterdir(), key=major, reverse=True):
            if major(version_dir) < MIN_NODE_MAJOR:
                continue
            node = version_dir / "bin" / "node"
            run_js = version_dir / "lib" / "node_modules" / "@salesforce" / "cli" / "bin" / "run.js"
            if node.is_file() and run_js.is_file():
                return [str(node), str(run_js)]

    found = shutil.which("sf")
    if found:
        return [found]

    raise SalesforceError(
        "No usable Salesforce CLI found. Install it under Node >= 22 "
        "(`nvm install 24 && npm install -g @salesforce/cli`) or set HERMES_SF_BIN."
    )


def _env() -> dict[str, str]:
    env = dict(os.environ)
    if CA_BUNDLE.is_file():
        env.setdefault("NODE_EXTRA_CA_CERTS", str(CA_BUNDLE))
    # Keep the CLI from emitting its telemetry notice into the JSON we parse.
    env.setdefault("SF_DISABLE_TELEMETRY", "true")
    return env


def run(args: list[str], *, timeout: int = 180) -> dict:
    """Run `sf <args> --json` and return the parsed result payload."""
    argv = _resolve_sf() + args + ["--json"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, env=_env(), timeout=timeout
    )

    # The CLI returns JSON on both success and failure, but a crash at import (bad
    # Node) produces a stack trace on stderr and nothing parseable on stdout.
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        detail = (proc.stderr or proc.stdout or "").strip()
        if "markAsUncloneable" in detail:
            detail = (
                "the Salesforce CLI is running on Node < 22 and crashed at import. "
                "Reinstall it under Node >= 22.\n" + detail
            )
        elif "SELF_SIGNED_CERT_IN_CHAIN" in detail:
            detail = (
                f"TLS interception blocked the call and {CA_BUNDLE} is missing or stale. "
                "Regenerate with: security find-certificate -a -p "
                f"/Library/Keychains/System.keychain > {CA_BUNDLE}\n" + detail
            )
        raise SalesforceError(f"Salesforce CLI returned no JSON: {detail}")

    if payload.get("status") != 0:
        raise SalesforceError(
            payload.get("message") or f"sf {' '.join(args)} failed", payload
        )

    return payload.get("result", {})


def query(soql: str, *, org: str, tooling: bool = False) -> list[dict]:
    """Run a SOQL query and return its records."""
    args = ["data", "query", "--query", soql, "--target-org", org]
    if tooling:
        args.append("--use-tooling-api")
    return run(args).get("records", [])


def query_one(soql: str, *, org: str) -> dict | None:
    """Return the single matching record, or None. Raises if the query is ambiguous."""
    records = query(soql, org=org)
    if len(records) > 1:
        raise SalesforceError(f"expected at most 1 record, got {len(records)}: {soql}")
    return records[0] if records else None


def count(soql: str, *, org: str) -> int:
    """Run a `SELECT COUNT(Id)` query and return the number."""
    records = query(soql, org=org)
    return int(records[0]["expr0"]) if records else 0


def create(sobject: str, fields: dict, *, org: str) -> str:
    """Insert one record, returning its Id."""
    result = run(
        ["data", "create", "record", "--sobject", sobject, "--target-org", org,
         "--values", _values(fields)]
    )
    return result["id"]


def upsert(sobject: str, external_id_field: str, fields: dict, *, org: str) -> str:
    """Upsert one record on an external id field, returning its Id.

    This is what makes re-submitting the same order_id idempotent rather than
    duplicating an opportunity.
    """
    import csv
    import tempfile

    # `sf data upsert` is file-based; there is no inline-values form.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".csv", delete=False, newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields))
        writer.writeheader()
        writer.writerow(fields)
        csv_path = handle.name

    try:
        result = run(
            ["data", "upsert", "bulk", "--sobject", sobject, "--file", csv_path,
             "--external-id", external_id_field, "--target-org", org, "--wait", "10"]
        )
    finally:
        os.unlink(csv_path)

    if result.get("numberRecordsFailed"):
        raise SalesforceError(f"upsert failed for {sobject}", result)
    return result.get("id", "")


def _values(fields: dict) -> str:
    """Render a dict as the CLI's `--values` format: key=value pairs, quoted."""
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        parts.append(f'{key}="{rendered}"')
    return " ".join(parts)
