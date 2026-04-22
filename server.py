"""
WIPO Madrid Gazette Refusal Tracker
Flask web app that pulls the latest Madrid Gazette refusal data, filters
for Latin America & Caribbean designations, and pushes leads to Google Sheets.

Run locally:
    pip install -r requirements.txt
    export FLASK_APP=server.py
    python server.py

Deploy to Render:
    See README.md
"""
import csv
import io
import os
from datetime import datetime

from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, flash

from parser import fetch_latest_refusals, LATAM_MEMBERS
from sheets import push_to_sheet, SheetsError

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "change-me-in-production")

# In-memory cache of the most recent fetch so the UI can re-render without
# re-hitting WIPO. On Render's free tier this resets on cold start, which
# is fine for an on-demand tool.
_LAST_RESULTS = {"fetched_at": None, "rows": [], "source": None}


@app.route("/")
def index():
    return render_template(
        "index.html",
        latam_members=LATAM_MEMBERS,
        last=_LAST_RESULTS,
    )


@app.route("/fetch", methods=["POST"])
def fetch():
    """Trigger a fresh pull from the Gazette."""
    days = int(request.form.get("days", 7))
    countries = request.form.getlist("countries") or list(LATAM_MEMBERS.keys())
    refusal_types = request.form.getlist("types") or ["RFNT", "RFNP", "FINC"]

    try:
        rows, source = fetch_latest_refusals(
            days=days,
            countries=countries,
            refusal_types=refusal_types,
        )
    except Exception as exc:  # pragma: no cover - surfaced in UI
        flash(f"Could not reach WIPO data source: {exc}", "error")
        return redirect(url_for("index"))

    _LAST_RESULTS["fetched_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    _LAST_RESULTS["rows"] = rows
    _LAST_RESULTS["source"] = source

    flash(f"Fetched {len(rows)} refusals from {source}.", "success")
    return redirect(url_for("index"))


@app.route("/push-to-sheets", methods=["POST"])
def push_sheets():
    """Append current results to the configured Google Sheet."""
    if not _LAST_RESULTS["rows"]:
        flash("Nothing to push yet — run Fetch first.", "error")
        return redirect(url_for("index"))

    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        flash("GOOGLE_SHEET_ID env var is not set. See README.", "error")
        return redirect(url_for("index"))

    try:
        appended = push_to_sheet(sheet_id, _LAST_RESULTS["rows"])
    except SheetsError as exc:
        flash(f"Google Sheets error: {exc}", "error")
        return redirect(url_for("index"))

    flash(f"Pushed {appended} new rows to Google Sheets.", "success")
    return redirect(url_for("index"))


@app.route("/download.csv")
def download_csv():
    """Download current results as CSV."""
    if not _LAST_RESULTS["rows"]:
        flash("Nothing to download yet — run Fetch first.", "error")
        return redirect(url_for("index"))

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(_LAST_RESULTS["rows"][0].keys()))
    writer.writeheader()
    writer.writerows(_LAST_RESULTS["rows"])

    mem = io.BytesIO(buf.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(
        mem,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"wipo_refusals_{datetime.utcnow():%Y%m%d}.csv",
    )


@app.route("/cron-run", methods=["GET", "POST"])
def cron_run():
    """Single-shot automation endpoint.

    Hit this URL (GET or POST) with the header ``X-Cron-Token`` (or
    ``?token=...``) matching the ``CRON_TOKEN`` env var, and the app will:
        1. Fetch the last N days of refusals from WIPO
        2. Append the fresh ones to the configured Google Sheet

    Designed to be called by GitHub Actions (or any cron service) on a
    weekly schedule so no one has to open the UI.
    """
    expected = os.environ.get("CRON_TOKEN")
    if not expected:
        return jsonify(error="CRON_TOKEN env var not set on Render"), 500

    provided = (
        request.headers.get("X-Cron-Token")
        or request.args.get("token")
        or (request.get_json(silent=True) or {}).get("token")
    )
    if provided != expected:
        return jsonify(error="unauthorized"), 401

    days = int(request.args.get("days", 7))

    try:
        rows, source = fetch_latest_refusals(days=days)
    except Exception as exc:
        return jsonify(error=str(exc), stage="fetch"), 500

    _LAST_RESULTS["fetched_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    _LAST_RESULTS["rows"] = rows
    _LAST_RESULTS["source"] = source

    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        return jsonify(error="GOOGLE_SHEET_ID not set", fetched=len(rows)), 500

    try:
        appended = push_to_sheet(sheet_id, rows)
    except Exception as exc:
        return jsonify(error=str(exc), stage="sheets", fetched=len(rows)), 500

    return jsonify(
        ok=True,
        fetched=len(rows),
        appended_to_sheet=appended,
        source=source,
        at=_LAST_RESULTS["fetched_at"],
    )


@app.route("/healthz")
def healthz():
    """Render's health check endpoint."""
    return jsonify(status="ok", last_fetch=_LAST_RESULTS["fetched_at"])


@app.route("/api/refusals.json")
def api_refusals():
    """JSON API — useful for scheduled jobs and integrations."""
    return jsonify(_LAST_RESULTS)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
