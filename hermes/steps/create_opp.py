"""Step 3 - upsert the Opportunity.

Upserting on External_Id__c (a unique external-id field that already exists on
Opportunity) is what makes re-submitting the same order_id idempotent. There is no
separate dedupe pass to get wrong.

The org's Opportunity flow fires on insert and sets LeadSource, opportunity team
members, and related defaults. We deliberately do not populate those - fighting the
flow is how you end up with records the rest of the business does not recognise.
Booked-gate fields (NSO, MEDDPICC, contact roles) are not required at creation and
are left alone.
"""

from __future__ import annotations

from datetime import date, timedelta

from .. import sfcli
from ..errors import PRODUCT_NOT_FOUND, Rejection


def _record_type_id(developer_name: str, org: str) -> str:
    record = sfcli.query_one(
        "SELECT Id FROM RecordType WHERE SobjectType = 'Opportunity' "
        f"AND DeveloperName = '{developer_name}'",
        org=org,
    )
    if not record:
        raise Rejection(
            PRODUCT_NOT_FOUND, f"no Opportunity record type named {developer_name!r}"
        )
    return record["Id"]


def opportunity_name(normalised: dict, resolved: dict) -> str:
    unit_count = normalised["total_units"]
    plural = "Unit" if unit_count == 1 else "Units"
    return f"{resolved['billing_account_name']} - {unit_count} {plural} (Online Order)"


def create_opportunity(normalised: dict, resolved: dict, config: dict, org: str,
                       *, today: date | None = None) -> dict:
    """Upsert the Opportunity and return its Id plus whether it already existed."""
    defaults = config["opportunity_defaults"]
    today = today or date.today()
    close_date = today + timedelta(days=defaults["close_date_offset_days"])

    external_id_field = defaults["external_id_field"]
    order_id = normalised["order_id"]

    # Check first so the caller can report created-vs-reused honestly, and so a
    # re-run does not silently overwrite a quote a rep has since edited.
    existing = sfcli.query_one(
        f"SELECT Id, Name FROM Opportunity WHERE {external_id_field} = '{order_id}'",
        org=org,
    )

    fields = {
        "Name": opportunity_name(normalised, resolved),
        "AccountId": resolved["billing_account_id"],
        "RecordTypeId": _record_type_id(defaults["record_type_developer_name"], org),
        "StageName": defaults["stage_name"],
        "Type": defaults["type"],
        "CloseDate": close_date.isoformat(),
        defaults["unit_count_field"]: normalised["total_units"],
        external_id_field: order_id,
    }

    if existing:
        return {"id": existing["Id"], "already_existed": True, "fields": fields}

    opportunity_id = sfcli.create("Opportunity", fields, org=org)
    return {"id": opportunity_id, "already_existed": False, "fields": fields}
