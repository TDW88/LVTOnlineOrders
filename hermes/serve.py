#!/usr/bin/env python3
"""Local dev server: serves the portal AND runs orders through Hermes.

`python -m http.server` can only hand back files, so the portal's Submit Order button
had nowhere to send an order and fell back to downloading the payload. This server
closes that gap: static files as before, plus POST /api/order which runs the same
pipeline `hermes.hermes` runs and returns the created record ids.

    python3 -m hermes.serve            # http://localhost:8971
    python3 -m hermes.serve --port 9000
    python3 -m hermes.serve --dry-run  # resolve and plan, write nothing

Binds to 127.0.0.1 only, deliberately. This endpoint writes to Salesforce with your
CLI credentials and has no authentication of its own, so it must not be reachable from
the network. Do not change the bind address to expose it.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes import sfcli  # noqa: E402
from hermes.errors import Rejection  # noqa: E402
from hermes.hermes import load_config, run_order  # noqa: E402

MAX_BODY_BYTES = 256 * 1024


class HermesHandler(SimpleHTTPRequestHandler):
    """Static files, plus an order endpoint."""

    # Set by the partial() in main().
    org: str = "sandbox"
    dry_run: bool = False
    config: dict = {}

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.rstrip("/") != "/api/order":
            self.send_error(404, "no such endpoint")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json(400, {"ok": False, "error": "bad Content-Length"})
            return

        if length <= 0:
            self._json(400, {"ok": False, "error": "empty request body"})
            return
        if length > MAX_BODY_BYTES:
            self._json(413, {"ok": False, "error": "payload too large"})
            return

        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json(400, {"ok": False, "error": f"invalid JSON: {exc}"})
            return

        order_id = payload.get("order_id") if isinstance(payload, dict) else None
        print(f"[hermes] order received: {order_id}"
              f"{' (dry run)' if self.dry_run else ''}", flush=True)

        try:
            result = run_order(payload, self.config, self.org, dry_run=self.dry_run)
        except Rejection as rejection:
            # A refusal is an expected outcome, not a server fault. 422 so the portal can
            # tell "your order was refused, here is why" from "the server broke".
            print(f"[hermes] REJECTED {rejection}", flush=True)
            self._json(422, {"ok": False, **rejection.as_dict()})
            return
        except sfcli.SalesforceError as exc:
            print(f"[hermes] Salesforce error: {exc}", flush=True)
            self._json(502, {"ok": False, "error": f"Salesforce CLI error: {exc}"})
            return
        except Exception as exc:  # noqa: BLE001 - never kill the server on one bad order
            traceback.print_exc()
            self._json(500, {"ok": False, "error": f"unexpected failure: {exc}"})
            return

        self._json(200, {"ok": True, **_summarise(result)})

    def _json(self, status: int, body: dict) -> None:
        encoded = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def end_headers(self) -> None:
        # The portal is edited constantly during a demo; a cached index.html showing
        # yesterday's build is a needless source of confusion.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        # Quieten per-asset request logging so the order lines above stay readable.
        if "/api/order" in (self.path or ""):
            super().log_message(fmt, *args)


def _summarise(result: dict) -> dict:
    """Flatten a run_order result into what the portal needs to show."""
    if result.get("dry_run"):
        return {
            "dry_run": True,
            "order_id": result["normalised"]["order_id"],
            "billing_account": result["resolved"]["billing_account_name"],
            "total_units": result["normalised"]["total_units"],
            "planned_bundles": len(result["planned_bundles"]),
        }

    opportunity = result.get("opportunity") or {}
    quote = result.get("quote") or {}
    contact = result.get("contact") or {}
    return {
        "dry_run": False,
        "order_id": result.get("order_id"),
        "primary_contact": contact.get("name"),
        "primary_contact_id": contact.get("id"),
        "primary_contact_created": contact.get("created"),
        "provisioned": result.get("provisioned"),
        "opportunity_id": opportunity.get("Id"),
        "opportunity_name": opportunity.get("Name"),
        "account_name": (opportunity.get("Account") or {}).get("Name"),
        "stage": opportunity.get("StageName"),
        "quote_id": quote.get("Id"),
        "quote_name": quote.get("Name"),
        "quote_net_amount": result.get("quote_net_amount"),
        "line_count": result.get("line_count"),
        "priced": result.get("priced"),
        "already_existed": result.get("opportunity_already_existed"),
        "warnings": result.get("warnings") or [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=8971)
    parser.add_argument("--org", default=None, help="target org alias (default: from config)")
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve and plan every order, but write nothing")
    args = parser.parse_args(argv)

    config = load_config()
    org = args.org or config["org"]["alias"]

    handler = partial(HermesHandler, directory=str(REPO_ROOT))
    HermesHandler.org = org
    HermesHandler.dry_run = args.dry_run
    HermesHandler.config = config

    # Threading so a ~20s order does not stall the page's own asset requests.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"portal:  http://localhost:{args.port}/index.html")
    print(f"orders:  POST /api/order -> org '{org}'"
          f"{'  [DRY RUN - nothing will be written]' if args.dry_run else ''}")
    print("bound to 127.0.0.1 only; this endpoint writes to Salesforce", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
