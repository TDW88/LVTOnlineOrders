#!/usr/bin/env python3
"""Hermes - turn a portal order payload into a Salesforce Opportunity and CPQ Quote.

Deterministic by design. The portal collects information; this script turns that
information into records. The same payload always produces the same result, which is
what makes it testable and what keeps an AI out of the transaction path.

Usage:
    python3 -m hermes.hermes payloads/golden.json
    python3 -m hermes.hermes payloads/golden.json --dry-run
    python3 -m hermes.hermes payloads/golden.json --org sandbox

Exit codes:
    0  order created (or already existed - idempotent re-run)
    2  order rejected; no records created
    3  environment or unexpected failure
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # allow `python3 hermes/hermes.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes import sfcli
from hermes.errors import Rejection
from hermes.steps import create_opp, create_quote, insert_lines, resolve, validate, verify

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def load_config() -> dict:
    with CONFIG_PATH.open() as handle:
        return json.load(handle)


def run_order(payload: dict, config: dict, org: str, *, dry_run: bool = False) -> dict:
    """Run the full pipeline. Raises Rejection with zero records created on refusal."""
    # --- everything that can fail without writing anything happens first ---
    normalised = validate.validate(payload, config)
    resolve.assert_correct_org(config, org)
    resolved = resolve.resolve(normalised, org)
    planned = insert_lines.plan_lines(normalised, resolved, config)

    if dry_run:
        return {
            "dry_run": True,
            "normalised": normalised,
            "resolved": resolved,
            "planned_bundles": planned,
        }

    # --- from here on we are writing ---
    opportunity = create_opp.create_opportunity(normalised, resolved, config, org)

    quote_id = create_quote.find_existing_quote(opportunity["id"], org)
    reused_quote = quote_id is not None
    if not reused_quote:
        quote_id = create_quote.create_quote(
            normalised, resolved, opportunity["id"], config, org
        )
        insert_lines.insert_lines(quote_id, normalised, resolved, config, org)

    result = verify.verify(opportunity["id"], quote_id, org)
    result.update(
        {
            "dry_run": False,
            "order_id": normalised["order_id"],
            "opportunity_already_existed": opportunity["already_existed"],
            "quote_reused": reused_quote,
        }
    )
    return result


def _print_human(result: dict) -> None:
    if result.get("dry_run"):
        normalised = result["normalised"]
        resolved = result["resolved"]
        print("DRY RUN - nothing written\n")
        print(f"  order            {normalised['order_id']}")
        print(f"  billing account  {resolved['billing_account_name']} ({resolved['billing_account_id']})")
        print(f"  term             {normalised['term_months']} months")
        print(f"  total units      {normalised['total_units']}")
        print(f"  NDAA             {normalised['ndaa_compliant']}")
        print(f"  software         {normalised['software_packages'] or 'none'}")
        print(f"\n  {len(result['planned_bundles'])} bundle(s) would be created:")
        for group in result["planned_bundles"]:
            gen = " +generator" if group["needs_generator"] else ""
            print(f"    - {group['quantity']}x {group['unit_type']}{gen} "
                  f"at {group['location_account_name']} "
                  f"({len(group['option_ids'])} option lines)")
        return

    opportunity = result.get("opportunity") or {}
    quote = result.get("quote") or {}
    print("Order processed\n")
    print(f"  order            {result['order_id']}")
    print(f"  opportunity      {opportunity.get('Name')} ({opportunity.get('Id')})"
          f"{'  [existing]' if result['opportunity_already_existed'] else '  [created]'}")
    print(f"  stage            {opportunity.get('StageName')}")
    print(f"  account          {(opportunity.get('Account') or {}).get('Name')}")
    print(f"  units            {opportunity.get('of_Units_in_Pipeline__c')}")
    print(f"  quote            {quote.get('Name')} ({quote.get('Id')})"
          f"{'  [reused]' if result['quote_reused'] else '  [created]'}")
    print(f"  quote lines      {result['line_count']} "
          f"({result['head_line_count']} bundle head, {result['priced_line_count']} priced)")
    print(f"  term             {quote.get('SBQQ__SubscriptionTerm__c')} months")
    print(f"  CPQ list amount  {result['quote_list_amount']:,.2f}")
    print(f"  CPQ net amount   {result['quote_net_amount']:,.2f}"
          f"{'' if result['priced'] else '   [NOT PRICED]'}")
    print(f"  sum of lines     {result['line_total']:,.2f}")

    for warning in result.get("warnings", []):
        print(f"\n  WARNING: {warning}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("payload", type=Path, help="path to an order payload JSON file")
    parser.add_argument("--org", default=None, help="target org alias (default: from config)")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and resolve, then stop before writing anything")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="emit the raw result as JSON")
    args = parser.parse_args(argv)

    config = load_config()
    org = args.org or config["org"]["alias"]

    try:
        payload = json.loads(args.payload.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"could not read payload: {exc}", file=sys.stderr)
        return 3

    try:
        result = run_order(payload, config, org, dry_run=args.dry_run)
    except Rejection as rejection:
        if args.as_json:
            print(json.dumps(rejection.as_dict(), indent=2))
        else:
            print(f"REJECTED  {rejection}", file=sys.stderr)
            print("\nNo records were created.", file=sys.stderr)
        return 2
    except sfcli.SalesforceError as exc:
        print(f"Salesforce CLI error: {exc}", file=sys.stderr)
        return 3

    if args.as_json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_human(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
