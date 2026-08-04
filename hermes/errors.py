"""Rejection codes and the exception that carries them.

The contract with the portal is that a rejected order names exactly what was wrong
and leaves zero records behind. A rejected order a rep picks up is still faster than
today; a quote attached to the wrong account is a cleanup incident. So every failure
mode here is a refusal, never a guess.
"""

from __future__ import annotations


class Rejection(Exception):
    """Order refused. No records created."""

    def __init__(self, code: str, detail: str, field: str | None = None):
        self.code = code
        self.detail = detail
        self.field = field
        location = f" [{field}]" if field else ""
        super().__init__(f"{code}{location}: {detail}")

    def as_dict(self) -> dict:
        return {
            "rejected": True,
            "code": self.code,
            "field": self.field,
            "detail": self.detail,
        }


# Payload shape
INVALID_PAYLOAD = "INVALID_PAYLOAD"
MISSING_FIELD = "MISSING_FIELD"
INVALID_FIELD = "INVALID_FIELD"

# Account / location resolution
ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
ACCOUNT_AMBIGUOUS = "ACCOUNT_AMBIGUOUS"
LOCATION_NOT_FOUND = "LOCATION_NOT_FOUND"
LOCATION_AMBIGUOUS = "LOCATION_AMBIGUOUS"
LOCATION_PARENT_MISMATCH = "LOCATION_PARENT_MISMATCH"
LOCATION_NOT_MAPPED = "LOCATION_NOT_MAPPED"

# Catalog
PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
UNSUPPORTED_OPTION = "UNSUPPORTED_OPTION"

# Environment / preconditions
WRONG_ORG = "WRONG_ORG"
PARTIAL_CREATION = "PARTIAL_CREATION"
