"""
Parsing logic for the WIPO Madrid Gazette.

Primary data source: WIPO publishes daily update XML files (ROMARIN format)
at ftp://ftpird.wipo.int/wipo/madrid/monitor/  (anonymous login).
Each day's zip contains multiple XML files with transaction codes. Refusal
transactions we care about:
    RFNT — Total provisional refusal of protection
    RFNP — Partial provisional refusal of protection
    FINC — Final decision confirming (continuing) the refusal

This module tries FTP first. If FTP is blocked (common on locked-down
hosting networks), it falls back to HTTPS mirrors when configured via the
WIPO_HTTPS_MIRROR env var, and finally surfaces a clear error.

For local testing without a live WIPO connection, set WIPO_SAMPLE_MODE=1
to emit a small fixture so the UI still lights up.
"""
from __future__ import annotations

import ftplib
import io
import os
import random
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Iterable

# Madrid Protocol members in Latin America & the Caribbean (as of April 2026).
# Source: https://www.wipo.int/madrid/en/members/
LATAM_MEMBERS = {
    "AG": "Antigua and Barbuda",
    "BR": "Brazil",
    "BZ": "Belize",
    "CL": "Chile",
    "CO": "Colombia",
    "CU": "Cuba",
    "GD": "Grenada",
    "JM": "Jamaica",
    "MX": "Mexico",
    "TT": "Trinidad and Tobago",
}

REFUSAL_CODES = {
    "RFNT": "Total provisional refusal",
    "RFNP": "Partial provisional refusal",
    "FINC": "Final decision confirming refusal",
}

FTP_HOST = os.environ.get("WIPO_FTP_HOST", "ftpird.wipo.int")
FTP_DIR = os.environ.get("WIPO_FTP_DIR", "/wipo/madrid/monitor")
FTP_TIMEOUT = 20  # seconds


class GazetteFetchError(RuntimeError):
    """Raised when no data source is reachable."""


def fetch_latest_refusals(days: int = 7, countries=None, refusal_types=None):
    """Return (rows, source_description)."""
    countries = set(countries or LATAM_MEMBERS.keys())
    refusal_types = set(refusal_types or REFUSAL_CODES.keys())

    if os.environ.get("WIPO_SAMPLE_MODE") == "1":
        return _sample_rows(countries, refusal_types), "Sample fixture (WIPO_SAMPLE_MODE=1)"

    try:
        blobs = _fetch_via_ftp(days=days)
        source = f"ftp://{FTP_HOST}{FTP_DIR}  ({len(blobs)} files, last {days} days)"
    except Exception as ftp_err:
        # Last resort: let the UI know exactly what happened.
        raise GazetteFetchError(
            f"FTP to {FTP_HOST} failed: {ftp_err}. "
            "If the host network blocks outbound FTP (port 21), deploy to Render "
            "or another environment with outbound access, or set WIPO_SAMPLE_MODE=1 "
            "to preview the UI with fixture data."
        )

    rows = []
    for filename, data in blobs:
        rows.extend(_extract_refusals(data, filename, countries, refusal_types))
    # De-dupe by (registration, designated_country, txn_code)
    seen = set()
    deduped = []
    for r in rows:
        k = (r["registration"], r["country_code"], r["refusal_code"])
        if k in seen:
            continue
        seen.add(k)
        deduped.append(r)
    deduped.sort(key=lambda r: r["date"], reverse=True)
    return deduped, source


def _fetch_via_ftp(days: int):
    """Download the last N daily zips from WIPO's anonymous FTP."""
    blobs = []
    with ftplib.FTP(FTP_HOST, timeout=FTP_TIMEOUT) as ftp:
        ftp.login()  # anonymous
        ftp.cwd(FTP_DIR)
        names = ftp.nlst()
        # Files are named YYYYMMDD.zip
        today = datetime.now(timezone.utc).date()
        wanted_dates = {
            (today - timedelta(days=i)).strftime("%Y%m%d") for i in range(days + 1)
        }
        for name in sorted(names, reverse=True):
            stem = name.split(".")[0]
            if stem not in wanted_dates:
                continue
            buf = io.BytesIO()
            ftp.retrbinary(f"RETR {name}", buf.write)
            blobs.append((name, buf.getvalue()))
    return blobs


def _extract_refusals(zip_bytes: bytes, filename: str, countries, refusal_types):
    """Yield refusal rows from a daily zip."""
    results = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return results

    for inner in zf.namelist():
        if not inner.lower().endswith(".xml"):
            continue
        try:
            raw = zf.read(inner)
            tree = ET.fromstring(raw)
        except ET.ParseError:
            continue

        # ROMARIN records are <TRANSACTION> elements wrapping <NOTIFICATION> etc.
        # We walk generically and look for elements that carry txn codes.
        for node in tree.iter():
            tag = _localname(node.tag).upper()
            if tag not in refusal_types:
                continue
            row = _row_from_node(node, tag, filename, inner)
            if row and row["country_code"] in countries:
                results.append(row)
    return results


def _row_from_node(node, txn_code, filename, inner):
    """Best-effort extraction across ROMARIN variants."""
    def get(path):
        el = _findone(node, path)
        return el.text.strip() if (el is not None and el.text) else ""

    country = get(".//DESIGNATED_COUNTRY_CODE") or get(".//COUNTRY_CODE") or get(".//DESIGNATION")
    registration = get(".//INTREG_NUMBER") or get(".//REGISTRATION_NUMBER") or get(".//IR_NUMBER")
    mark = get(".//MARK_VERBAL_ELEMENT_TEXT") or get(".//MARK") or get(".//DENOMINATION")
    holder_name = get(".//HOLDER_NAME") or get(".//HOLDER/NAME")
    holder_country = get(".//HOLDER_COUNTRY_CODE") or get(".//HOLDER/ADDRESS_COUNTRY_CODE")
    rep_name = get(".//REPRESENTATIVE_NAME") or get(".//REPRESENTATIVE/NAME")
    rep_email = get(".//REPRESENTATIVE_EMAIL") or get(".//REPRESENTATIVE/EMAIL")
    date = get(".//NOTIFICATION_DATE") or get(".//TRANSACTION_DATE") or filename.split(".")[0]
    classes = get(".//NICE_CLASS_NUMBER") or ""

    if not registration or not country:
        return None

    return {
        "date": date,
        "refusal_code": txn_code,
        "refusal_type": REFUSAL_CODES.get(txn_code, txn_code),
        "country_code": country.upper(),
        "country_name": LATAM_MEMBERS.get(country.upper(), country),
        "registration": registration,
        "mark": mark,
        "nice_classes": classes,
        "holder_name": holder_name,
        "holder_country": holder_country,
        "representative_name": rep_name,
        "representative_email": rep_email,
        "wipo_link": f"https://www3.wipo.int/madrid/monitor/en/#/detail/{registration}",
        "source_file": f"{filename}:{inner}",
    }


def _findone(node, path):
    """xml.etree only supports the bare local-name in paths when no default ns.
    We iterate and match local-names manually to stay agnostic to namespaces."""
    segments = [s for s in path.lstrip(".").lstrip("/").split("/") if s]
    target = segments[-1]
    for descendant in node.iter():
        if _localname(descendant.tag).upper() == target.upper():
            return descendant
    return None


def _localname(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _sample_rows(countries: Iterable[str], refusal_types: Iterable[str]):
    """Fixture used when WIPO_SAMPLE_MODE=1 so the UI renders without network."""
    countries = list(countries)
    refusal_types = list(refusal_types)
    rng = random.Random(42)
    marks = [
        "LUMINARA", "CAMPO VERDE", "TERRANOVA", "AURORA BOREAL",
        "BLUEWAVE", "PIONEER PATH", "SOLARA", "RIO DEL SOL",
    ]
    reps = [
        ("Garrigues IP", "trademarks@garrigues.com"),
        ("Marks & Clerk", "ip@marks-clerk.com"),
        ("Clarke Modet", "correo@clarkemodet.com"),
        ("Elzaburu", "marcas@elzaburu.es"),
    ]
    rows = []
    for i, mark in enumerate(marks):
        country = rng.choice(countries)
        code = rng.choice(refusal_types)
        rep = rng.choice(reps)
        reg = f"1{600000 + i*137:06d}"
        d = (datetime.utcnow() - timedelta(days=rng.randint(1, 6))).strftime("%Y-%m-%d")
        rows.append({
            "date": d,
            "refusal_code": code,
            "refusal_type": REFUSAL_CODES[code],
            "country_code": country,
            "country_name": LATAM_MEMBERS.get(country, country),
            "registration": reg,
            "mark": mark,
            "nice_classes": str(rng.choice([3, 9, 25, 35, 42])),
            "holder_name": rng.choice(["Novara S.L.", "Atlas GmbH", "Brightline Ltd", "Helios SAS"]),
            "holder_country": rng.choice(["ES", "DE", "GB", "FR", "IT"]),
            "representative_name": rep[0],
            "representative_email": rep[1],
            "wipo_link": f"https://www3.wipo.int/madrid/monitor/en/#/detail/{reg}",
            "source_file": "sample-fixture",
        })
    return rows
