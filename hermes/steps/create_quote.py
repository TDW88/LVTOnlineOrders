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
                 config: dict, org: str, *, today: date | None = None) -> str:
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
    return sfcli.create("SBQQ__Quote__c", fields, org=org)


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
