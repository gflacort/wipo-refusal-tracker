"""
Google Sheets integration — append fresh refusal rows to a sheet the user owns.

Auth approach: a Google service account. You create one in GCP, download the
JSON key, paste the JSON into the GOOGLE_SERVICE_ACCOUNT_JSON env var on Render,
and share your sheet with the service account's email address.

Env vars:
    GOOGLE_SERVICE_ACCOUNT_JSON  — full JSON of the service account key
    GOOGLE_SHEET_ID              — the ID from the sheet URL
    GOOGLE_SHEET_TAB             — optional, defaults to "Refusals"
"""
from __future__ import annotations

import json
import os
from typing import Iterable

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:  # pragma: no cover - surfaced at runtime
    gspread = None
    Credentials = None


class SheetsError(RuntimeError):
    pass


COLUMNS = [
    "date",
    "refusal_code",
    "refusal_type",
    "country_code",
    "country_name",
    "registration",
    "mark",
    "nice_classes",
    "holder_name",
    "holder_country",
    "representative_name",
    "representative_email",
    "wipo_link",
    "source_file",
]


def push_to_sheet(sheet_id: str, rows: Iterable[dict]) -> int:
    """Append rows to the configured sheet. Returns number of new rows appended
    (de-duped against registration + country_code + refusal_code already present)."""
    if gspread is None:
        raise SheetsError(
            "gspread is not installed. Add gspread + google-auth to requirements.txt."
        )

    key_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not key_json:
        raise SheetsError(
            "GOOGLE_SERVICE_ACCOUNT_JSON env var not set. Paste your service account JSON there."
        )

    try:
        info = json.loads(key_json)
    except json.JSONDecodeError as exc:
        raise SheetsError(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    gc = gspread.authorize(creds)

    try:
        sh = gc.open_by_key(sheet_id)
    except Exception as exc:
        raise SheetsError(
            f"Could not open sheet {sheet_id}. "
            f"Did you share it with the service account email? ({exc})"
        )

    tab_name = os.environ.get("GOOGLE_SHEET_TAB", "Refusals")
    try:
        ws = sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=1000, cols=len(COLUMNS))
        ws.append_row(COLUMNS, value_input_option="RAW")

    existing = ws.get_all_values()
    header = existing[0] if existing else []
    if not header:
        ws.append_row(COLUMNS, value_input_option="RAW")
        header = COLUMNS

    # Build a set of already-present keys so we don't duplicate.
    try:
        idx_reg = header.index("registration")
        idx_country = header.index("country_code")
        idx_code = header.index("refusal_code")
    except ValueError:
        idx_reg = idx_country = idx_code = None

    seen = set()
    if idx_reg is not None:
        for row in existing[1:]:
            if len(row) > max(idx_reg, idx_country, idx_code):
                seen.add((row[idx_reg], row[idx_country], row[idx_code]))

    new_rows = []
    for r in rows:
        key = (r.get("registration", ""), r.get("country_code", ""), r.get("refusal_code", ""))
        if key in seen:
            continue
        seen.add(key)
        new_rows.append([r.get(col, "") for col in COLUMNS])

    if new_rows:
        ws.append_rows(new_rows, value_input_option="RAW")
    return len(new_rows)
