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
    head_unit_option = _head_unit_option(normalised, config)

    module_options = [
        config["software_modules"][name]["option_id"]
        for name in normalised["software_packages"]
    ]

    # Fees apply to every order rather than being customer-selectable.
    fee_options = [
        fee["option_id"] for key, fee in config.get("bundle_option_fees", {}).items()
        if not key.startswith("_")
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
                        *fee_options,
                    ],
                    "standalone_fees": _standalone_fees_for(unit_type, config),
                }
            )
    return planned


def _head_unit_option(normalised: dict, config: dict) -> str:
    """Resolve the HEADUNIT option from the camera the customer actually chose.

    The portal's camera skus are the real Salesforce HEADUNIT product names, so they map
    straight onto bundle options. The ndaa_compliant boolean is only a fallback for older
    payloads that carry no camera selection: deriving the head unit from it put a generic
    Dahua base unit on every non-NDAA order regardless of what was picked.
    """
    sku = normalised.get("camera_sku")
    if sku:
        entry = config["head_units_by_sku"].get(sku)
        if not entry:
            # validate() should have caught this; refuse rather than substitute a camera.
            raise Rejection(
                PRODUCT_NOT_FOUND, f"no HEADUNIT option pinned for camera sku {sku!r}"
            )
        return entry["option_id"]

    fallback_key = "ndaa" if normalised["ndaa_compliant"] else "non_ndaa"
    return config["head_units_fallback"][fallback_key]["option_id"]


def _standalone_fees_for(unit_type: str, config: dict) -> list[dict]:
    """Fees that go on the quote as top-level lines rather than bundle options.

    Round-trip shipping has no ProductOption under this bundle, and the product differs by
    form factor: mobile units ship at $2,000, wall/pole at $1,000.
    """
    fees = []
    for key, fee in config.get("standalone_fees", {}).items():
        if key.startswith("_"):
            continue
        variant = (fee.get("by_unit_type") or {}).get(unit_type)
        if variant:
            fees.append({**variant, "fee_key": key})
    return fees


def expected_net_amount(normalised: dict, resolved: dict, config: dict) -> float:
    """What the quote should total if CPQ priced our lines correctly, at list.

    This is a verification expectation, not an input to pricing - nothing computed here
    is ever written to Salesforce. It exists because "the total stopped changing" turned
    out to be an unsound signal for CPQ having finished: pricing lands incrementally, and
    a pause between lines long enough to span two polls reads as settled. Knowing the
    number we are waiting for removes the guesswork.

    Value sits on three kinds of line; the bundle head, BASEUNIT and HEADUNIT all list
    at $0:

      * FORM FACTOR SUBSCRIPTION - monthly, extended over the term
      * MODULE                   - monthly, extended over the term
      * fees (setup, shipping)   - one-time, NOT extended over the term

    Getting that last distinction wrong is a 12x error on the fee portion, so fees are
    summed separately rather than folded into the monthly figure.
    """
    term = normalised["term_months"]
    modules = normalised["software_packages"]

    bundle_fee_per_unit = sum(
        fee["list_price"] for key, fee in config.get("bundle_option_fees", {}).items()
        if not key.startswith("_")
    )

    total = 0.0
    for group in plan_lines(normalised, resolved, config):
        quantity = group["quantity"]
        form_factor_key = "mobile" if group["unit_type"] == "mobile" else "wall"
        monthly = config["form_factor_subscriptions"][form_factor_key]["list_price"]
        monthly += sum(config["software_modules"][name]["list_price"] for name in modules)
        total += monthly * term * quantity

        # One-time fees: bundle options plus standalone lines, neither term-extended.
        total += bundle_fee_per_unit * quantity
        total += sum(fee["list_price"] for fee in group["standalone_fees"]) * quantity

    return round(total, 2)


def quote_has_lines(quote_id: str, org: str) -> bool:
    """Whether the quote already carries any lines.

    Guards the re-run path. A run that dies between creating the quote and inserting lines
    leaves an empty quote behind; without this check the next run adopts it and reports a
    $0 order as processed.
    """
    return sfcli.count(
        f"SELECT COUNT(Id) FROM SBQQ__QuoteLine__c WHERE SBQQ__Quote__c = '{quote_id}'",
        org=org,
    ) > 0


def insert_lines(quote_id: str, normalised: dict, resolved: dict, config: dict,
                 org: str, group_id: str | None = None) -> dict:
    """Insert bundle head lines and their option lines. Returns a creation manifest.

    The manifest is returned even on failure (via the raised error's context) so that
    cleanup after a mid-insert failure is one query rather than a hunt.
    """
    planned = plan_lines(normalised, resolved, config)

    all_option_ids = sorted({oid for group in planned for oid in group["option_ids"]})
    option_to_product = _option_products(all_option_ids, org)

    bundle_product_id = config["bundle"]["product_id"]
    created: list[str] = []

    # Every line joins the quote's line group, so the editor shows them grouped rather
    # than as a flat list. Omitted entirely when there is no group.
    group_field = {"SBQQ__Group__c": group_id} if group_id else {}

    try:
        for group in planned:
            head_line_id = sfcli.create(
                "SBQQ__QuoteLine__c",
                {
                    "SBQQ__Quote__c": quote_id,
                    "SBQQ__Product__c": bundle_product_id,
                    "SBQQ__Quantity__c": group["quantity"],
                    "SBQQ__SubscriptionTerm__c": normalised["term_months"],
                    **group_field,
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
                        **group_field,
                    },
                    org=org,
                )
                created.append(line_id)

            # Top-level lines: no SBQQ__RequiredBy__c and no SBQQ__ProductOption__c,
            # because these products are not options of this bundle. Matches how the
            # org's existing quotes carry them. No SubscriptionTerm either - they are
            # one-time charges and must not be extended.
            for fee in group["standalone_fees"]:
                line_id = sfcli.create(
                    "SBQQ__QuoteLine__c",
                    {
                        "SBQQ__Quote__c": quote_id,
                        "SBQQ__Product__c": fee["product_id"],
                        "SBQQ__Quantity__c": group["quantity"],
                        **group_field,
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
