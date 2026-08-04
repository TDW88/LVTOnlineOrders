"""Step 6 - read back what we created and report it.

This step exists because "the CLI returned success" is weaker evidence than it looks
in CPQ. Lines insert cleanly and still price wrong if the bundle structure is off, and
the head/BASEUNIT/HEADUNIT lines all list at $0 so a broken quote looks plausible.

Timing matters here, in two ways that both produce wrong numbers:

  * CPQ prices inserted lines *asynchronously*. A verify that runs immediately after
    insert reads zeros and reports a healthy quote as broken.
  * CPQ prices lines *incrementally*. A partially-priced quote reports a non-zero total
    that is simply too low - observed live: the same order read 68,000 mid-calculation
    and 92,000 once settled, the gap being two module lines that had not landed yet.

So "non-zero" is not a sufficient stopping condition. We wait for the net amount to be
non-zero AND unchanged across consecutive reads before believing it.

Price semantics, which are easy to get wrong:
  SBQQ__NetPrice__c  - per unit, extended over the subscription term
                       (2833.33/mo x 12 = 34000)
  SBQQ__NetTotal__c  - the above x quantity. This is what sums to the quote total.
  SBQQ__NetAmount__c - quote-level total. Authoritative; prefer it over any sum we do.
"""

from __future__ import annotations

import time

from .. import sfcli

# CPQ's async pricing usually settles in a few seconds. Poll rather than sleep a fixed
# amount so the fast path stays fast, and require the total to repeat before trusting
# it so we do not read a half-calculated quote.
PRICING_POLL_ATTEMPTS = 12
PRICING_POLL_DELAY_SECONDS = 1.5
REQUIRED_STABLE_READS = 1


def _as_number(value) -> float:
    return float(value or 0)


def _read_quote(quote_id: str, org: str) -> dict | None:
    return sfcli.query_one(
        "SELECT Id, Name, SBQQ__Primary__c, SBQQ__SubscriptionTerm__c, SBQQ__Status__c, "
        "SBQQ__NetAmount__c, SBQQ__ListAmount__c, SBQQ__CustomerAmount__c, "
        "SBQQ__LineItemCount__c, SBQQ__PrimaryContact__c, SBQQ__PrimaryContact__r.Name "
        f"FROM SBQQ__Quote__c WHERE Id = '{quote_id}'",
        org=org,
    )


def wait_for_pricing(quote_id: str, org: str,
                     expected_net: float | None = None) -> tuple[dict | None, bool]:
    """Poll until CPQ has finished pricing the quote.

    Returns (quote, settled). `settled` is False if we ran out of attempts, meaning the
    reported total should not be trusted.

    When `expected_net` is known we wait for that exact figure, which is the only
    reliable stopping condition: CPQ prices incrementally, and a pause between lines
    longer than the poll interval makes a partial total look stable. Observed live -
    the same order settled at 80,000 on one run and 92,000 on another, the difference
    being a single module line that had not landed when two consecutive reads agreed.

    Without an expectation we fall back to stability, which is weaker and can under-report.
    """
    quote = _read_quote(quote_id, org)
    previous = None
    stable_reads = 0

    for _ in range(PRICING_POLL_ATTEMPTS):
        current = _as_number(quote.get("SBQQ__NetAmount__c")) if quote else 0.0

        if expected_net is not None:
            if abs(current - expected_net) <= 0.01:
                return quote, True
        elif current > 0 and current == previous:
            stable_reads += 1
            if stable_reads >= REQUIRED_STABLE_READS:
                return quote, True
        else:
            stable_reads = 0

        previous = current
        time.sleep(PRICING_POLL_DELAY_SECONDS)
        quote = _read_quote(quote_id, org)

    return quote, False


def verify(opportunity_id: str, quote_id: str, org: str,
           expected_net: float | None = None) -> dict:
    opportunity = sfcli.query_one(
        "SELECT Id, Name, StageName, Type, CloseDate, AccountId, Account.Name, "
        "of_Units_in_Pipeline__c, External_Id__c, RecordType.Name "
        f"FROM Opportunity WHERE Id = '{opportunity_id}'",
        org=org,
    )

    quote, settled = wait_for_pricing(quote_id, org, expected_net)
    priced = settled

    lines = sfcli.query(
        "SELECT Id, SBQQ__Product__r.Name, SBQQ__Product__r.ProductCode, "
        "SBQQ__Quantity__c, SBQQ__ListPrice__c, SBQQ__NetPrice__c, SBQQ__NetTotal__c, "
        "SBQQ__RequiredBy__c, SBQQ__ProductOption__c "
        f"FROM SBQQ__QuoteLine__c WHERE SBQQ__Quote__c = '{quote_id}' "
        "ORDER BY SBQQ__RequiredBy__c NULLS FIRST",
        org=org,
    )

    # Read the primary contact back rather than trusting the write.
    primary_roles = sfcli.query(
        "SELECT ContactId, Contact.Name, Contact.Email, Role, IsPrimary "
        f"FROM OpportunityContactRole WHERE OpportunityId = '{opportunity_id}' "
        "AND IsPrimary = true",
        org=org,
    )

    priced_lines = [ln for ln in lines if _as_number(ln.get("SBQQ__NetTotal__c")) > 0]
    # Sum SBQQ__NetTotal__c (already quantity-extended). Multiplying NetPrice by
    # quantity would double-count on any line CPQ has already extended.
    line_total = sum(_as_number(ln.get("SBQQ__NetTotal__c")) for ln in lines)

    quote_net = _as_number(quote.get("SBQQ__NetAmount__c")) if quote else 0.0
    quote_list = _as_number(quote.get("SBQQ__ListAmount__c")) if quote else 0.0

    warnings = []
    if lines and not priced:
        detail = (
            f"expected {expected_net} but read {quote_net}"
            if expected_net is not None else f"net amount currently {quote_net}"
        )
        warnings.append(
            f"CPQ pricing had not settled after "
            f"{PRICING_POLL_ATTEMPTS * PRICING_POLL_DELAY_SECONDS:.0f}s ({detail}). "
            "This total is likely partial - re-read the quote, or open it in the Quote "
            "Line Editor and save to force a recalculation, before trusting any figure."
        )
    elif expected_net is not None and abs(quote_net - expected_net) > 0.01:
        warnings.append(
            f"quote net ({quote_net}) does not match the expected list total "
            f"({expected_net}). The bundle structure or a pinned price may be wrong."
        )
    if quote and not quote.get("SBQQ__Primary__c"):
        warnings.append("quote is not flagged primary.")

    head_lines = [ln for ln in lines if not ln.get("SBQQ__RequiredBy__c")]
    if not head_lines:
        warnings.append("no bundle head line found - the bundle structure is wrong.")

    if priced and abs(line_total - quote_net) > 0.01:
        warnings.append(
            f"sum of line totals ({line_total}) does not match the quote net amount "
            f"({quote_net}). Trust the quote; investigate the lines."
        )

    # v1 quotes at list. Any gap means a discount crept in somewhere it should not have.
    if priced and abs(quote_list - quote_net) > 0.01:
        warnings.append(
            f"quote list amount ({quote_list}) differs from net ({quote_net}), implying "
            "a discount was applied. v1 is meant to quote at list price only."
        )

    quote_contact_name = ((quote or {}).get("SBQQ__PrimaryContact__r") or {}).get("Name")
    opp_contact_name = (primary_roles[0].get("Contact") or {}).get("Name") if primary_roles else None

    # A quote whose primary contact disagrees with the opportunity's is the kind of
    # inconsistency nobody notices until the quote document goes out addressed wrongly.
    if quote_contact_name and opp_contact_name and quote_contact_name != opp_contact_name:
        warnings.append(
            f"quote primary contact ({quote_contact_name}) differs from the "
            f"opportunity's primary contact ({opp_contact_name})."
        )

    return {
        "opportunity": opportunity,
        "quote": quote,
        "priced": priced,
        "primary_contact_opportunity": opp_contact_name,
        "primary_contact_quote": quote_contact_name,
        "expected_net_amount": expected_net,
        "line_count": len(lines),
        "head_line_count": len(head_lines),
        "priced_line_count": len(priced_lines),
        "line_total": round(line_total, 2),
        "quote_net_amount": quote_net,
        "quote_list_amount": quote_list,
        "lines": lines,
        "warnings": warnings,
    }
