"""Resolve the portal's contact name to a Salesforce Contact.

This is the one place Hermes creates a record it did not find, and that is a deliberate
exception to the find-or-reject rule the account and location steps follow. The reason:
the golden-path account has no Contacts at all, and neither do most location accounts.
Rejecting an order because the person placing it is not already in Salesforce would
refuse essentially every order, which defeats the point.

Duplicate risk is managed by matching on email within the account hierarchy first, and
by only ever creating against the billing account. If the payload carries no name we
link nothing and say so - inventing a contact from an email prefix would be worse than
leaving the field empty for a rep to fill.

Accounts and locations still reject rather than create. Getting those wrong attaches a
quote to the wrong company; getting a contact wrong creates a duplicate person, which is
a smaller and more fixable problem.
"""

from __future__ import annotations

from .. import sfcli


def split_name(full_name: str) -> tuple[str | None, str]:
    """Split a single display name into (first, last).

    Salesforce requires LastName and nothing else, so a mononym becomes the last name
    rather than being padded with a placeholder. Middle names stay with the last name so
    nothing is silently dropped: "Ana Maria Reyes" -> ("Ana", "Maria Reyes").
    """
    parts = full_name.strip().split()
    if not parts:
        return None, ""
    if len(parts) == 1:
        return None, parts[0]
    return parts[0], " ".join(parts[1:])


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def resolve_contact(contact: dict, billing_account_id: str, org: str) -> dict | None:
    """Find or create the Contact for this order. Returns None if there is nothing to link.

    Returns {"id", "name", "created"} so callers can report whether a person was added.
    """
    name = (contact or {}).get("name") or ""
    email = ((contact or {}).get("email") or "").strip()

    if not name.strip():
        return None

    first_name, last_name = split_name(name)

    # Prefer an email match anywhere in the account's hierarchy - the same person is
    # often filed against a location account rather than the parent.
    if email:
        matches = sfcli.query(
            "SELECT Id, Name, AccountId FROM Contact "
            f"WHERE Email = '{_escape(email)}' "
            f"AND (AccountId = '{billing_account_id}' "
            f"OR Account.ParentId = '{billing_account_id}') "
            "ORDER BY CreatedDate LIMIT 1",
            org=org,
        )
        if matches:
            return {"id": matches[0]["Id"], "name": matches[0]["Name"], "created": False}

    # Fall back to a name match on the billing account before creating, so repeated
    # orders from someone who gave no email do not pile up duplicates.
    name_filter = f"LastName = '{_escape(last_name)}'"
    if first_name:
        name_filter += f" AND FirstName = '{_escape(first_name)}'"
    matches = sfcli.query(
        f"SELECT Id, Name FROM Contact WHERE {name_filter} "
        f"AND AccountId = '{billing_account_id}' ORDER BY CreatedDate LIMIT 1",
        org=org,
    )
    if matches:
        return {"id": matches[0]["Id"], "name": matches[0]["Name"], "created": False}

    fields = {
        "LastName": last_name,
        "AccountId": billing_account_id,
    }
    if first_name:
        fields["FirstName"] = first_name
    if email:
        fields["Email"] = email
    phone = ((contact or {}).get("phone") or "").strip()
    if phone:
        fields["Phone"] = phone

    contact_id = sfcli.create("Contact", fields, org=org)
    return {"id": contact_id, "name": name.strip(), "created": True}


def link_primary_contact_role(opportunity_id: str, contact_id: str, org: str) -> bool:
    """Attach the contact to the opportunity as its primary contact role.

    OpportunityContactRole is the org's canonical primary-contact mechanism (562 of 586
    roles are flagged primary). Returns True if a role was created, False if one already
    existed - re-running an order must not stack duplicate roles.
    """
    existing = sfcli.query(
        "SELECT Id, ContactId, IsPrimary FROM OpportunityContactRole "
        f"WHERE OpportunityId = '{opportunity_id}'",
        org=org,
    )
    for role in existing:
        if role["ContactId"].startswith(contact_id[:15]):
            return False

    # Salesforce demotes any existing primary automatically when a new primary is set,
    # so there is no need to clear the old one first.
    sfcli.create(
        "OpportunityContactRole",
        {
            "OpportunityId": opportunity_id,
            "ContactId": contact_id,
            "Role": "Primary Contact",
            "IsPrimary": True,
        },
        org=org,
    )
    return True
