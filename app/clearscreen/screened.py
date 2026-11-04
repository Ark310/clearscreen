# -*- coding: utf-8 -*-
"""Our customer, read from a record's Screened_Fields and Screened_Values: comma-separated
field names and the matching comma-separated values, e.g.

  Screened_Fields  "Beneficiary Name,Beneficiary Country,Beneficiary City,Beneficiary Province"
  Screened_Values  "ACME LTD,Canada,Toronto,Ontario"

parse_screened -> name, entity_type, dob, country, state, city, identification (None if absent).

Every alert screens one party. Its name is the field ending in "Name" (Name, Beneficiary
Name, Beneficiary Bank Name, Cheque Name: ...) or First Name + Initial + Last Name. Country /
Province / State / City / DOB / ID fields are recognised by the words in their names; street,
post code, alias and instruction fields are not used. Entity type: Individual for First/Last
Name layouts, Organization for bank layouts, otherwise unknown.

A value may itself contain commas (an address): when there are more values than fields, the
extra pieces are given to the first street / address / instruction field, so the fields after
it keep their own values.
"""
import re

_FREE_TEXT = ("address", "street", "instruction")


def _kind(field: str) -> str | None:
    f = field.lower().strip()
    if f == "first name":
        return "first"
    if f in ("initial", "middle name", "middle initial"):
        return "middle"
    if f == "last name":
        return "last"
    if "alias" in f:
        return None
    if "birth" in f or re.search(r"\bdob\b", f):
        return "dob"
    if any(w in f for w in ("passport", "identification", "id number", "national id", "registration")):
        return "identification"
    if "country" in f:
        return "country"
    if "province" in f or re.search(r"\bstate\b", f):
        return "state"
    if "city" in f:
        return "city"
    if f.rstrip(":").strip().endswith("name"):
        return "name"
    return None


def _align(fields: list[str], values: list[str]) -> list[str]:
    """One value per field; surplus comma pieces join the first free-text field."""
    extra = len(values) - len(fields)
    if extra <= 0:
        return values + [""] * -extra
    at = next((i for i, f in enumerate(fields) if any(w in f.lower() for w in _FREE_TEXT)), None)
    if at is None:                                   # nowhere sensible: keep the leading values
        return values[:len(fields)]
    return values[:at] + [",".join(values[at:at + extra + 1])] + values[at + extra + 1:]


def parse_screened(fields, values) -> dict:
    names = [f.strip() for f in str(fields or "").split(",") if f.strip()]
    vals = _align(names, [v.strip() for v in str(values or "").split(",")])
    got: dict[str, str] = {}
    for field, value in zip(names, vals):
        kind = _kind(field)
        if kind and value and kind not in got:
            got[kind] = value
    name = got.get("name") or " ".join(got[k] for k in ("first", "middle", "last") if got.get(k)) or None
    lowered = [n.lower() for n in names]
    entity_type = ("Individual" if "first name" in lowered
                   else "Organization" if any("bank name" in n or "interbank" in n for n in lowered) else None)
    return dict(name=name, entity_type=entity_type, dob=got.get("dob"), country=got.get("country"),
                state=got.get("state"), city=got.get("city"), identification=got.get("identification"))
