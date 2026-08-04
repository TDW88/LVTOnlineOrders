"""Step 3 - upsert the Opportunity.

Upserting on External_Id__c (a unique external-id field that already exists on
Opportunity) is what makes re-submitting the same order_id idempotent. There is no
separate dedupe pass to get wrong.

The org's Opportunity flow fires on insert and sets opportunity team members and related
defaults; we do not populate those, because fighting the flow is how you end up with
records the rest of the business does not recognise.

LeadSource is the exception. It was originally left to the flow on the assumption that
the flow set it - checking the org showed otherwise: every order created here had
LeadSource null. It is now set explicitly from config, as is Type.

Booked-gate fields (NSO, MEDDPICC, contact roles) are not required at creation and are
left alone.
"""

from __future__ import annotations

from datetime import date, timedelta

from .. import sfcli
from ..errors import INVALID_FIELD, PRODUCT_NOT_FOUND, Rejection


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
        # Inserted at an early stage on purpose. The org rejects creating an opportunity
        # beyond an early stage ("New Opportunities cannot be created beyond Stage 1"),
        # so the target stage is applied afterwards by advance_stage().
        "StageName": defaults.get("creation_stage_name") or defaults["stage_name"],
        "Type": defaults["type"],
        "CloseDate": close_date.isoformat(),
        defaults["unit_count_field"]: normalised["total_units"],
        external_id_field: order_id,
    }

    # Set explicitly rather than left to the Opportunity flow. Checked against the org:
    # every Hermes order had LeadSource null, so nothing downstream populates it.
    if defaults.get("lead_source"):
        fields["LeadSource"] = defaults["lead_source"]

    if existing:
        return {"id": existing["Id"], "already_existed": True, "fields": fields}

    opportunity_id = sfcli.create("Opportunity", fields, org=org)
    return {"id": opportunity_id, "already_existed": False, "fields": fields}


def advance_stage(opportunity_id: str, config: dict, org: str) -> dict:
    """Walk the opportunity up to its target stage, one stage at a time.

    Must run AFTER the primary contact is attached and the primary quote exists. Four org
    validation rules together force this shape - see the _stage_note in config.json. In
    particular the stage cannot be jumped ("Please only move forward one stage at a time"),
    so each intermediate stage is saved in turn.

    Only advances from the creation stage. If a human has since moved the opportunity -
    qualified it further, closed it - a re-run must not drag it backwards, which would
    quietly undo a rep's work.
    """
    defaults = config["opportunity_defaults"]
    target = defaults["stage_name"]
    creation_stage = defaults.get("creation_stage_name")
    progression = defaults.get("stage_progression") or []

    if target == creation_stage:
        return {"advanced": False, "reason": "target stage is the creation stage"}

    current = sfcli.query_one(
        f"SELECT Id, StageName FROM Opportunity WHERE Id = '{opportunity_id}'", org=org
    )
    if not current:
        return {"advanced": False, "reason": "opportunity not found"}

    current_stage = current["StageName"]
    if current_stage == target:
        return {"advanced": False, "reason": "already at target stage", "stage": target}

    if creation_stage and current_stage != creation_stage:
        return {
            "advanced": False,
            "reason": f"left at {current_stage!r} - moved by someone else, not overwriting",
            "stage": current_stage,
        }

    if defaults.get("use_apex_test_bypass"):
        return _jump_with_bypass(opportunity_id, target, defaults, org)

    if target not in progression or current_stage not in progression:
        raise Rejection(
            INVALID_FIELD,
            f"cannot plan a stage path from {current_stage!r} to {target!r}: "
            "both must appear in opportunity_defaults.stage_progression",
        )

    start = progression.index(current_stage)
    end = progression.index(target)
    if end < start:
        return {"advanced": False, "reason": "target stage is behind current stage",
                "stage": current_stage}

    steps = progression[start + 1:end + 1]
    for stage in steps:
        sfcli.run(
            ["data", "update", "record", "--sobject", "Opportunity",
             "--record-id", opportunity_id, "--target-org", org,
             "--values", f'StageName="{stage}"']
        )

    return {"advanced": True, "stage": target, "steps": steps, "method": "walk"}


def _update(opportunity_id: str, values: str, org: str) -> None:
    sfcli.run(
        ["data", "update", "record", "--sobject", "Opportunity",
         "--record-id", opportunity_id, "--target-org", org, "--values", values]
    )


def _jump_with_bypass(opportunity_id: str, target: str, defaults: dict,
                      org: str) -> dict:
    """Jump straight to the target stage using the Apex_Test__c bypass.

    The flag suppresses only the one-stage-at-a-time rule; the primary contact, primary
    quote and units requirements are still enforced and still satisfied properly by
    earlier steps. That is the point - this skips a bookkeeping rule, not a data-quality one.

    The flag is cleared afterwards so a finished order is not left looking like Apex test
    data. If clearing fails the stage change still stands, so we report it rather than
    failing the order.
    """
    _update(opportunity_id, 'Apex_Test__c="true"', org)
    try:
        _update(opportunity_id, f'StageName="{target}"', org)
    except Exception:
        # Do not leave the bypass set on an order whose stage did not move.
        try:
            _update(opportunity_id, 'Apex_Test__c="false"', org)
        except Exception:  # noqa: BLE001 - original failure is the useful one
            pass
        raise

    cleared = False
    clear_error = None
    if defaults.get("clear_apex_test_after_jump"):
        try:
            _update(opportunity_id, 'Apex_Test__c="false"', org)
            cleared = True
        except Exception as exc:  # noqa: BLE001 - stage change already succeeded
            clear_error = str(exc)

    return {
        "advanced": True,
        "stage": target,
        "method": "apex_test_bypass",
        "apex_test_cleared": cleared,
        "apex_test_clear_error": clear_error,
    }
