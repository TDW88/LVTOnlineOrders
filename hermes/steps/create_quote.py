"""Step 4 - create the primary Quote on the Opportunity.

Almost every field on SBQQ__Quote__c is nillable because CPQ derives them. We set
only what identifies the quote commercially: account, opportunity, pricebook, term,
and the primary flag. Pricing is left entirely to CPQ - v1 quotes at list, so there
is nothing for us to compute and nothing to get wrong.
"""

from __future__ import annotations

from datetime import date

from .. import sfcli


def create_quote(normalised: dict, resolved: dict, opportunity_id: str,
                 config: dict, org: str, *, today: date | None = None,
                 contact_id: str | None = None) -> str:
    """Create the primary quote and return its Id."""
    today = today or date.today()

    fields = {
        "SBQQ__Account__c": resolved["billing_account_id"],
        "SBQQ__Opportunity2__c": opportunity_id,
        "SBQQ__PriceBook__c": config["pricebook"]["id"],
        "SBQQ__Primary__c": True,
        "SBQQ__SubscriptionTerm__c": normalised["term_months"],
        "SBQQ__StartDate__c": today.isoformat(),
    }
    # By far the most-used contact field in this org (5,751 quotes carry it) and what
    # appears on the generated quote document.
    if contact_id:
        fields["SBQQ__PrimaryContact__c"] = contact_id

    return sfcli.create("SBQQ__Quote__c", fields, org=org)


def set_primary_contact(quote_id: str, contact_id: str, org: str) -> None:
    """Set the primary contact on an existing quote, for idempotent re-runs."""
    sfcli.run(
        ["data", "update", "record", "--sobject", "SBQQ__Quote__c",
         "--record-id", quote_id, "--target-org", org,
         "--values", f'SBQQ__PrimaryContact__c="{contact_id}"']
    )


def create_line_group(quote_id: str, resolved: dict, org: str) -> dict:
    """Create the quote line group that all lines hang off.

    Mirrors what "Add Group" does in the Quote Line Editor. Field names bear no
    resemblance to their UI labels, which is worth stating plainly:

        QLE label "Billing Account"  ->  Billing_Account__c
        QLE label "VMS Account"      ->  End_User__c

    Both point at the order's account, which is also the org's own convention: of 10,015
    existing groups, 9,807 populate Billing_Account__c and 9,800 populate End_User__c, and
    on real quotes the two hold the same Account Id.

    Billing_Account__c carries a lookup filter that rejects any account unless the group's
    own Account_Filter_Id__c matches the account's Account_Filter_Id__c. Without it the
    insert fails with the unhelpful "Value does not exist or does not match filter
    criteria", so the id is read off the account and copied onto the group. Every account
    in the org has one, including ones Hermes provisions (org automation fills it in), so
    this is safe on both paths.

    Ship_to_Account__c is deliberately NOT set. Real groups do populate it, but a
    validation rule ("Invalid Ship_To Account - Quote Line Group") rejects our location
    account, and nothing in the request needs it. Consequence: the group's
    Address_Missing__c formula will be true, so the editor may flag a missing address.

    Grouping also has to be switched on at the quote (SBQQ__LineItemsGrouped__c) or the
    editor shows a flat line list and the group is invisible - but that is done by
    enable_grouping() *after* the lines exist. Setting it here does not survive: CPQ's
    recalculation on line insert puts it back to false.
    """
    location = (resolved.get("locations") or [{}])[0]
    account_id = resolved["billing_account_id"]
    account_name = resolved["billing_account_name"]
    location_name = location.get("account_name") or account_name

    # Existing quotes in this org name groups "<billing account> - <location>".
    name = f"{account_name} - {location_name}"[:80]

    account = sfcli.query_one(
        f"SELECT Id, Account_Filter_Id__c FROM Account WHERE Id = '{account_id}'", org=org
    )
    account_filter_id = (account or {}).get("Account_Filter_Id__c")

    fields = {
        "SBQQ__Quote__c": quote_id,
        "Name": name,
        "SBQQ__Number__c": 10,
        # QLE labels: "Billing Account" and "VMS Account" respectively.
        "Billing_Account__c": account_id,
        "End_User__c": account_id,
    }
    if account_filter_id is not None:
        fields["Account_Filter_Id__c"] = int(account_filter_id)
    else:
        # Would fail the lookup filter. Better a group without the billing account than
        # no group at all, and the omission is reported rather than hidden.
        fields.pop("Billing_Account__c")

    group_id = sfcli.create("SBQQ__QuoteLineGroup__c", fields, org=org)

    return {
        "id": group_id,
        "name": name,
        "billing_account_set": "Billing_Account__c" in fields,
        "vms_account_set": True,
    }


def enable_grouping(quote_id: str, org: str) -> bool:
    """Flag the quote as grouped, so the editor renders groups rather than a flat list.

    Must run AFTER the lines are inserted. Setting it during group creation looked fine
    and then silently reverted - CPQ's recalculation on line insert resets it to false.
    Returns whether it stuck, read back rather than assumed.
    """
    sfcli.run(
        ["data", "update", "record", "--sobject", "SBQQ__Quote__c",
         "--record-id", quote_id, "--target-org", org,
         "--values", 'SBQQ__LineItemsGrouped__c="true"']
    )
    record = sfcli.query_one(
        f"SELECT SBQQ__LineItemsGrouped__c FROM SBQQ__Quote__c WHERE Id = '{quote_id}'",
        org=org,
    )
    return bool((record or {}).get("SBQQ__LineItemsGrouped__c"))


def find_existing_group(quote_id: str, org: str) -> dict | None:
    """Return the quote's existing line group, so a re-run does not add a second."""
    record = sfcli.query_one(
        "SELECT Id, Name FROM SBQQ__QuoteLineGroup__c "
        f"WHERE SBQQ__Quote__c = '{quote_id}' ORDER BY SBQQ__Number__c LIMIT 1",
        org=org,
    )
    return {"id": record["Id"], "name": record["Name"]} if record else None


def find_existing_quote(opportunity_id: str, org: str) -> str | None:
    """Return the primary quote already on this opportunity, if any.

    Used on re-runs so we extend rather than duplicate. A second primary quote on the
    same opportunity is the kind of mess that is tedious to unpick by hand.
    """
    record = sfcli.query_one(
        "SELECT Id FROM SBQQ__Quote__c "
        f"WHERE SBQQ__Opportunity2__c = '{opportunity_id}' AND SBQQ__Primary__c = true",
        org=org,
    )
    return record["Id"] if record else None
