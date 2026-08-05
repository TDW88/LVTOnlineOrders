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

import json
import re
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


NOTES_BEGIN = "--- LVT Online Order configuration (managed by Hermes) ---"
NOTES_END = "--- end LVT Online Order configuration ---"

# Coded values the portal sends, mapped to the wording it shows the customer. Anything not
# listed falls back to a title-cased version of the raw value, so an unmapped new option
# still reads sensibly instead of being dropped.
VALUE_LABELS = {
    "safety-recorded": "For your safety, area being recorded",
    "parking-recorded": "Parking lot being recorded",
    "classical": "Royalty free classical music",
    "intelligentDeterrence": "Intelligent Deterrence",
    "strobe": "Strobe Light",
    "floodlight": "Flood Light",
    "audible": "Trespassing audible",
    "loiter": "Loiter",
    "immediate": "Immediate",
    "yes": "Yes",
    "no": "No",
    "on": "On",
    "off": "Off",
    "day": "Day",
    "night": "Night",
}


ACRONYMS = {"gps": "GPS", "zip": "ZIP", "ars": "ARS", "lpr": "LPR", "id": "ID",
            "ndaa": "NDAA", "sku": "SKU"}


def _label(key: str) -> str:
    """camelCase / snake_case KEY -> readable label.

    Only ever applied to keys. Applying it to values mangled free text - a placement map
    named "Cartersville-Yard-North.pdf" came out as "Cartersville- Yard- North.pdf".
    """
    if key in VALUE_LABELS:
        return VALUE_LABELS[key]
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", str(key).replace("_", " ")).strip()
    words = [ACRONYMS.get(w.lower(), w) for w in spaced.split()]
    if words:
        words[0] = words[0] if words[0] in ACRONYMS.values() else words[0].capitalize()
    return " ".join(words)


def _render_value(value, indent: int) -> list[str]:
    """Render a value as readable lines. Generic on purpose.

    Rendering structurally rather than field-by-field means a new key the portal starts
    sending shows up in the notes automatically. A hand-written template would silently
    omit it, which is the worse failure - a rep would have no idea something was missing.
    """
    pad = "  " * indent
    lines: list[str] = []

    if isinstance(value, dict):
        # A dict of booleans reads best as a list of what is switched on.
        if value and all(isinstance(v, bool) for v in value.values()):
            enabled = [_label(k) for k, v in value.items() if v]
            return [f"{pad}{', '.join(enabled) if enabled else 'None'}"]
        for key, sub in value.items():
            if sub is None or sub == "" or sub == [] or sub == {}:
                continue
            if isinstance(sub, (dict, list)):
                nested = _render_value(sub, indent + 1)
                if len(nested) == 1 and not nested[0].strip().startswith("-"):
                    lines.append(f"{pad}{_label(key)}: {nested[0].strip()}")
                else:
                    lines.append(f"{pad}{_label(key)}:")
                    lines.extend(nested)
            else:
                lines.append(f"{pad}{_label(key)}: {_scalar(sub)}")
        return lines or [f"{pad}(none)"]

    if isinstance(value, list):
        for index, item in enumerate(value, start=1):
            if isinstance(item, dict):
                lines.append(f"{pad}{index}.")
                lines.extend(_render_value(item, indent + 1))
            else:
                lines.append(f"{pad}- {_scalar(item)}")
        return lines or [f"{pad}(none)"]

    return [f"{pad}{_scalar(value)}"]


def _scalar(value) -> str:
    """Render a leaf value. Strings pass through verbatim unless they are a known code -
    free text like a filename or a description must not be reformatted.
    """
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, str):
        return VALUE_LABELS.get(value, value)
    return str(value)


def render_configuration(site_config: dict) -> str:
    """Render site_config as readable text rather than JSON."""
    return "\n".join(_render_value(site_config, 0))


def set_configuration_notes(opportunity_id: str, payload: dict, org: str) -> dict:
    """Write the order's configuration section into Opportunity.Notes__c.

    The block is fenced by markers and only that block is ever rewritten, so anything a
    rep types into Notes__c survives a re-submission. Blindly overwriting the field would
    destroy their work - it is a general-purpose notes field, not ours alone.

    Written via the REST helper: the JSON contains quotes and newlines, which the CLI's
    --values form cannot carry.
    """
    site_config = payload.get("site_config")
    if not site_config:
        return {"written": False, "reason": "payload carries no site_config"}

    rendered = render_configuration(site_config)
    block = f"{NOTES_BEGIN}\n{rendered}\n{NOTES_END}"

    record = sfcli.query_one(
        f"SELECT Id, Notes__c FROM Opportunity WHERE Id = '{opportunity_id}'", org=org
    )
    existing = (record or {}).get("Notes__c") or ""

    if NOTES_BEGIN in existing and NOTES_END in existing:
        head = existing.split(NOTES_BEGIN)[0]
        tail = existing.split(NOTES_END, 1)[1]
        updated = f"{head}{block}{tail}"
    elif existing.strip():
        updated = f"{existing.rstrip()}\n\n{block}"
    else:
        updated = block

    # Notes__c holds 100,000 characters. Truncate the rendered config rather than let the
    # write fail, and say so in the field so nobody reads a partial config as complete.
    limit = 100_000
    if len(updated) > limit:
        marker = "\n[configuration truncated to fit Notes__c]\n"
        updated = updated[: limit - len(marker) - len(NOTES_END) - 1] + marker + NOTES_END

    sfcli.update_fields("Opportunity", opportunity_id, {"Notes__c": updated}, org=org)
    return {"written": True, "characters": len(block), "replaced_existing_block":
            NOTES_BEGIN in existing}


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
