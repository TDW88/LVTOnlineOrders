"""Step 5 - insert the bundle quote lines.

CPQ bundle structure: one head line for the bundle product with no SBQQ__RequiredBy__c,
then one line per selected option pointing back at the head via SBQQ__RequiredBy__c and
naming its SBQQ__ProductOption__c.

Two facts about this catalog that make errors quiet rather than loud:

  * The bundle head, every BASEUNIT and every HEADUNIT list at $0. All recurring value
    sits on FORM FACTOR SUBSCRIPTION and MODULE lines. A structurally wrong insert can
    therefore produce a quote that renders fine and totals wrong.
  * Several SBQQ__ProductOption__c rows under this bundle share identical OptionalSKU
    names. Options are resolved by pinned Id only - matching on name would be a coin
    flip.

We insert one bundle per unit group rather than one per order so that a mixed order
(mobile plus wall, or differing generator choices) keeps its structure legible to a rep.
"""

from __future__ import annotations

from .. import sfcli
from ..errors import PRODUCT_NOT_FOUND, Rejection


def _option_products(option_ids: list[str], org: str) -> dict[str, str]:
    """Map each SBQQ__ProductOption__c Id to its OptionalSKU product Id.

    Fetched at runtime rather than pinned so the config carries one Id per option
    instead of two that could drift apart.
    """
    if not option_ids:
        return {}

    quoted = ", ".join(f"'{oid}'" for oid in option_ids)
    records = sfcli.query(
        "SELECT Id, SBQQ__OptionalSKU__c, SBQQ__OptionalSKU__r.Name "
        f"FROM SBQQ__ProductOption__c WHERE Id IN ({quoted})",
        org=org,
    )

    found = {r["Id"]: r["SBQQ__OptionalSKU__c"] for r in records}
    missing = [oid for oid in option_ids if oid not in found]
    if missing:
        raise Rejection(
            PRODUCT_NOT_FOUND,
            f"pinned product options not found in this org: {missing}. "
            "A sandbox refresh may have invalidated hermes/config.json.",
        )
    return found


def plan_lines(normalised: dict, resolved: dict, config: dict) -> list[dict]:
    """Work out which options each unit group needs. Pure function - no org access.

    Returns one entry per bundle to insert, each with its option Ids. Separated from
    insertion so the mapping can be unit-tested and dry-run without touching the org.

    Iterates the *resolved* locations because they carry the Salesforce account Ids
    alongside the normalised unit data.
    """
    head_key = "ndaa" if normalised["ndaa_compliant"] else "non_ndaa"
    head_unit_option = config["head_units"][head_key]["option_id"]

    module_options = [
        config["software_modules"][name]["option_id"]
        for name in normalised["software_packages"]
    ]

    planned = []
    for location in resolved["locations"]:
        for unit in location["units"]:
            unit_type = unit["unit_type"]

            if unit_type == "mobile":
                base_key = "mobile_with_generator" if unit["needs_generator"] else "mobile"
            else:
                base_key = "wall"

            form_factor_key = "mobile" if unit_type == "mobile" else "wall"

            planned.append(
                {
                    "location_account_id": location["account_id"],
                    "location_account_name": location.get("account_name"),
                    "unit_type": unit_type,
                    "quantity": unit["quantity"],
                    "needs_generator": unit["needs_generator"],
                    "option_ids": [
                        config["base_units"][base_key]["option_id"],
                        head_unit_option,
                        config["form_factor_subscriptions"][form_factor_key]["option_id"],
                        *module_options,
                    ],
                }
            )
    return planned


def insert_lines(quote_id: str, normalised: dict, resolved: dict, config: dict,
                 org: str) -> dict:
    """Insert bundle head lines and their option lines. Returns a creation manifest.

    The manifest is returned even on failure (via the raised error's context) so that
    cleanup after a mid-insert failure is one query rather than a hunt.
    """
    planned = plan_lines(normalised, resolved, config)

    all_option_ids = sorted({oid for group in planned for oid in group["option_ids"]})
    option_to_product = _option_products(all_option_ids, org)

    bundle_product_id = config["bundle"]["product_id"]
    created: list[str] = []

    try:
        for group in planned:
            head_line_id = sfcli.create(
                "SBQQ__QuoteLine__c",
                {
                    "SBQQ__Quote__c": quote_id,
                    "SBQQ__Product__c": bundle_product_id,
                    "SBQQ__Quantity__c": group["quantity"],
                    "SBQQ__SubscriptionTerm__c": normalised["term_months"],
                },
                org=org,
            )
            created.append(head_line_id)

            for option_id in group["option_ids"]:
                line_id = sfcli.create(
                    "SBQQ__QuoteLine__c",
                    {
                        "SBQQ__Quote__c": quote_id,
                        "SBQQ__Product__c": option_to_product[option_id],
                        "SBQQ__ProductOption__c": option_id,
                        "SBQQ__RequiredBy__c": head_line_id,
                        "SBQQ__Quantity__c": group["quantity"],
                        "SBQQ__SubscriptionTerm__c": normalised["term_months"],
                    },
                    org=org,
                )
                created.append(line_id)
    except Exception as exc:  # noqa: BLE001 - re-raised with cleanup context attached
        raise Rejection(
            "PARTIAL_CREATION",
            f"line insertion failed after creating {len(created)} lines on quote "
            f"{quote_id}. Created line Ids: {created}. Underlying error: {exc}",
        ) from exc

    return {"quote_id": quote_id, "bundles": len(planned), "line_ids": created}
