# -*- coding: utf-8 -*-
"""Read whitelist records from an Excel workbook: every sheet with a ClientID column, columns
named as in WL_Records (ClientID, Unique_ID, Screened_Fields, Screened_Values, Database_Screened,
Screened_Date, Matched_Details, Analyst_Allow_Reasoning). Rows missing a required field are
returned as problems, not records."""
from openpyxl import load_workbook

COLUMNS = ["ClientID", "Unique_ID", "Screened_Fields", "Screened_Values", "Database_Screened",
           "Screened_Date", "Matched_Details", "Analyst_Allow_Reasoning"]
REQUIRED = COLUMNS[:-1]


def _text(v):
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return s or None


def read_records(path):
    """(records, problems): records as dicts of COLUMNS; problems as (sheet, Excel row, missing fields)."""
    records, problems = [], []
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            it = ws.iter_rows(values_only=True)
            header = [str(h).strip() if h is not None else None for h in next(it, ())]
            if "ClientID" not in header:
                continue
            pos = {c: header.index(c) for c in COLUMNS if c in header}
            for n, row in enumerate(it, start=2):
                if not row or all(v in (None, "") for v in row):
                    continue
                rec = {c: _text(row[pos[c]]) if c in pos and pos[c] < len(row) else None for c in COLUMNS}
                missing = [c for c in REQUIRED if not rec[c]]
                if missing:
                    problems.append((ws.title, n, missing))
                else:
                    records.append(rec)
    finally:
        wb.close()
    return records, problems
