"""Step 1 - validate the payload shape.

Pure function of the payload: no network, no org access. Everything checkable
without Salesforce is checked here so that obvious garbage never reaches a DML
statement.
"""

from __future__ import annotations

import re
from typing import Any

from ..errors import (
    INVALID_FIELD,
    INVALID_PAYLOAD,
    MISSING_FIELD,
    UNSUPPORTED_OPTION,
    Rejection,
)

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ZIP_RE = re.compile(r"^\d{5}$")

VALID_UNIT_TYPES = {"mobile", "wall"}
VALID_TERMS = {12, 24, 36}


def _require(container: Any, key: str, path: str) -> Any:
    if not isinstance(container, dict) or key not in container:
        raise Rejection(MISSING_FIELD, f"{path} is required", field=path)
    return container[key]


def _require_uuid(container: dict, key: str, path: str) -> str:
    value = _require(container, key, path)
    if not isinstance(value, str) or not UUID_RE.match(value):
        raise Rejection(INVALID_FIELD, f"{path} must be a UUID, got {value!r}", field=path)
    return value


def validate(payload: dict, config: dict) -> dict:
    """Validate the payload and return a normalised copy.

    Normalising here means later steps never re-parse: unit quantities are ints,
    software packages are deduplicated, and the flattened unit list is precomputed.
    """
    if not isinstance(payload, dict):
        raise Rejection(INVALID_PAYLOAD, "payload must be a JSON object")

    order_id = _require(payload, "order_id", "order_id")
    if not isinstance(order_id, str) or not order_id.strip():
        raise Rejection(INVALID_FIELD, "order_id must be a non-empty string", field="order_id")

    submitted_by = _require(payload, "submitted_by", "submitted_by")
    lvt_customer_id = _require_uuid(submitted_by, "lvt_customer_id", "submitted_by.lvt_customer_id")

    order = _require(payload, "order", "order")

    term = _require(order, "term_months", "order.term_months")
    if term not in VALID_TERMS:
        raise Rejection(
            INVALID_FIELD,
            f"order.term_months must be one of {sorted(VALID_TERMS)}, got {term!r}",
            field="order.term_months",
        )

    locations = _require(order, "locations", "order.locations")
    if not isinstance(locations, list) or not locations:
        raise Rejection(INVALID_FIELD, "order.locations must be a non-empty list", field="order.locations")

    known_modules = {name for name in config["software_modules"] if not name.startswith("_")}
    requested_modules = order.get("software_packages") or []
    if not isinstance(requested_modules, list):
        raise Rejection(
            INVALID_FIELD, "order.software_packages must be a list", field="order.software_packages"
        )

    # The portal's internal ids do not match CPQ names, and a legacy CPQ module
    # shares a name with one of them. Accept either form, resolve to the CPQ name,
    # and refuse anything we cannot place rather than dropping it silently.
    portal_id_to_name = {
        entry["portal_id"]: name
        for name, entry in config["software_modules"].items()
        if not name.startswith("_")
    }
    resolved_modules: list[str] = []
    for requested in requested_modules:
        if requested in known_modules:
            name = requested
        elif requested in portal_id_to_name:
            name = portal_id_to_name[requested]
        else:
            raise Rejection(
                UNSUPPORTED_OPTION,
                f"unknown software package {requested!r}; "
                f"expected one of {sorted(known_modules | set(portal_id_to_name))}",
                field="order.software_packages",
            )
        if name not in resolved_modules:
            resolved_modules.append(name)

    normalised_locations = []
    for index, location in enumerate(locations):
        path = f"order.locations[{index}]"
        lvt_location_id = _require_uuid(location, "lvt_location_id", f"{path}.lvt_location_id")

        zip_code = location.get("zip_code")
        if zip_code is not None and not ZIP_RE.match(str(zip_code)):
            raise Rejection(
                INVALID_FIELD, f"{path}.zip_code must be 5 digits, got {zip_code!r}",
                field=f"{path}.zip_code",
            )

        units = _require(location, "units", f"{path}.units")
        if not isinstance(units, list) or not units:
            raise Rejection(INVALID_FIELD, f"{path}.units must be a non-empty list", field=f"{path}.units")

        normalised_units = []
        for unit_index, unit in enumerate(units):
            unit_path = f"{path}.units[{unit_index}]"
            unit_type = _require(unit, "unit_type", f"{unit_path}.unit_type")
            if unit_type not in VALID_UNIT_TYPES:
                raise Rejection(
                    INVALID_FIELD,
                    f"{unit_path}.unit_type must be one of {sorted(VALID_UNIT_TYPES)}, got {unit_type!r}",
                    field=f"{unit_path}.unit_type",
                )

            quantity = _require(unit, "quantity", f"{unit_path}.quantity")
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
                raise Rejection(
                    INVALID_FIELD,
                    f"{unit_path}.quantity must be a positive integer, got {quantity!r}",
                    field=f"{unit_path}.quantity",
                )

            needs_generator = bool(unit.get("needs_generator", False))
            if needs_generator and unit_type != "mobile":
                raise Rejection(
                    UNSUPPORTED_OPTION,
                    f"{unit_path}: generators are only available on mobile units",
                    field=f"{unit_path}.needs_generator",
                )

            normalised_units.append(
                {"unit_type": unit_type, "quantity": quantity, "needs_generator": needs_generator}
            )

        normalised_locations.append(
            {
                "lvt_location_id": lvt_location_id,
                "zip_code": str(zip_code) if zip_code is not None else None,
                "units": normalised_units,
            }
        )

    hardware = order.get("hardware_options") or {}
    total_units = sum(u["quantity"] for loc in normalised_locations for u in loc["units"])

    return {
        "order_id": order_id.strip(),
        "lvt_customer_id": lvt_customer_id,
        "term_months": term,
        "po_number": order.get("po_number"),
        "locations": normalised_locations,
        "software_packages": resolved_modules,
        "ndaa_compliant": bool(hardware.get("ndaa_compliant", False)),
        "total_units": total_units,
    }
