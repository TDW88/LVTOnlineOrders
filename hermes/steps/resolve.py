"""Step 2 - resolve payload UUIDs to Salesforce record Ids.

Find-or-reject, never find-or-create. Resolution completes fully before any DML runs,
so a resolution failure means zero records were written.

Three independent checks per location, deliberately redundant:
  1. the location UUID resolves to exactly one Account
  2. that Account's ParentId is the resolved billing account
  3. a Location_Account_UUID_Mapping__c row binds the UUID pair

(2) exists because the mapping table in this sandbox is only partially aligned with
Account data - most rows reference UUIDs that resolve to nothing. The parent check
catches a stale or cross-wired mapping row that (3) alone would wave through.
"""

from __future__ import annotations

from .. import sfcli
from ..errors import (
    ACCOUNT_AMBIGUOUS,
    ACCOUNT_NOT_FOUND,
    LOCATION_AMBIGUOUS,
    LOCATION_NOT_FOUND,
    LOCATION_NOT_MAPPED,
    LOCATION_PARENT_MISMATCH,
    WRONG_ORG,
    Rejection,
)


def _escape(value: str) -> str:
    """Escape a value for inclusion in a SOQL string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def assert_correct_org(config: dict, org: str) -> None:
    """Refuse to run against an org we did not expect - especially production."""
    expected_id = config["org"]["expected_org_id"]
    records = sfcli.query(
        "SELECT Id, Name, IsSandbox FROM Organization LIMIT 1", org=org
    )
    if not records:
        raise Rejection(WRONG_ORG, "could not read Organization from the target org")

    actual = records[0]
    # Salesforce returns the 18-char Id; the configured value may be either form.
    if not actual["Id"].startswith(expected_id[:15]):
        raise Rejection(
            WRONG_ORG,
            f"connected to org {actual['Id']} ({actual['Name']}) but config expects {expected_id}",
        )
    if config["org"].get("expected_is_sandbox") and not actual.get("IsSandbox"):
        raise Rejection(
            WRONG_ORG,
            f"config expects a sandbox but {actual['Name']} reports IsSandbox=false",
        )


def resolve(normalised: dict, org: str) -> dict:
    """Resolve the billing account and every location account."""
    customer_uuid = normalised["lvt_customer_id"]

    accounts = sfcli.query(
        "SELECT Id, Name, RecordType.Name FROM Account "
        f"WHERE LvtCustomerId__c = '{_escape(customer_uuid)}'",
        org=org,
    )
    if not accounts:
        raise Rejection(
            ACCOUNT_NOT_FOUND,
            f"no Account with LvtCustomerId__c = {customer_uuid}",
            field="submitted_by.lvt_customer_id",
        )
    if len(accounts) > 1:
        raise Rejection(
            ACCOUNT_AMBIGUOUS,
            f"{len(accounts)} Accounts share LvtCustomerId__c = {customer_uuid}",
            field="submitted_by.lvt_customer_id",
        )

    billing_account = accounts[0]
    billing_id = billing_account["Id"]

    resolved_locations = []
    for index, location in enumerate(normalised["locations"]):
        path = f"order.locations[{index}].lvt_location_id"
        location_uuid = location["lvt_location_id"]

        matches = sfcli.query(
            "SELECT Id, Name, ParentId, RecordType.Name FROM Account "
            f"WHERE LvtLocationId__c = '{_escape(location_uuid)}'",
            org=org,
        )
        if not matches:
            raise Rejection(
                LOCATION_NOT_FOUND,
                f"no Account with LvtLocationId__c = {location_uuid}",
                field=path,
            )
        if len(matches) > 1:
            raise Rejection(
                LOCATION_AMBIGUOUS,
                f"{len(matches)} Accounts share LvtLocationId__c = {location_uuid}",
                field=path,
            )

        location_account = matches[0]
        parent_id = location_account.get("ParentId")
        if not parent_id or not parent_id.startswith(billing_id[:15]):
            raise Rejection(
                LOCATION_PARENT_MISMATCH,
                f"location {location_account['Name']} has ParentId {parent_id!r}, "
                f"expected the resolved billing account {billing_id} "
                f"({billing_account['Name']})",
                field=path,
            )

        mapping_count = sfcli.count(
            "SELECT COUNT(Id) FROM Location_Account_UUID_Mapping__c "
            f"WHERE Billing_Account_UUID__c = '{_escape(customer_uuid)}' "
            f"AND Location_Account_UUID__c = '{_escape(location_uuid)}'",
            org=org,
        )
        if mapping_count == 0:
            raise Rejection(
                LOCATION_NOT_MAPPED,
                f"no Location_Account_UUID_Mapping__c row binds customer {customer_uuid} "
                f"to location {location_uuid}",
                field=path,
            )

        resolved_locations.append(
            {
                **location,
                "account_id": location_account["Id"],
                "account_name": location_account["Name"],
            }
        )

    return {
        "billing_account_id": billing_id,
        "billing_account_name": billing_account["Name"],
        "locations": resolved_locations,
    }
