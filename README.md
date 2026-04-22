# WIPO Madrid Gazette — Refusal Tracker

A tiny Flask app that pulls the latest WIPO Madrid Gazette refusal data, filters for Latin America & Caribbean Madrid Protocol members (AG, BR, BZ, CL, CO, CU, GD, JM, MX, TT), and pushes each lead — mark, IR number, holder, representative email — into a Google Sheet so you can run outreach.

## What you'll set up

1. A free Render web service (runs the app 24/7)
2. A Google service account (lets the app write to your Sheet)
3. A Google Sheet (where leads land)

Total time: ~15 minutes.

---

## Step 1 — Get the code onto GitHub

Render deploys from a Git repo. You can use GitHub (free).

```bash
# From inside the unzipped folder
git init
git add .
git commit -m "Initial commit"
# Create a new repo on github.com, then:
git remote add origin https://github.com/YOUR_USERNAME/wipo-refusal-tracker.git
git branch -M main
git push -u origin main
```

If you'd rather not use Git: Render also supports "Deploy from a public repo" — fork it to your own GitHub once it's pushed, or use Render's "Upload" flow for static sites (not applicable here since we're a web service).

---

## Step 2 — Create the Google service account

This is what lets the app write to your Sheet without using your personal login.

1. Go to <https://console.cloud.google.com/> and create (or pick) a project.
2. In the left sidebar: **APIs & Services → Library**. Enable both:
   - **Google Sheets API**
   - **Google Drive API**
3. Left sidebar: **APIs & Services → Credentials → Create credentials → Service account**.
   - Name it anything (e.g. `wipo-tracker`).
   - Skip the role step (not needed for Sheets).
   - Click **Done**.
4. Click the service account you just made → **Keys** tab → **Add key → Create new key → JSON**. A `.json` file downloads. **Keep this private** — it's a password.
5. Open the JSON file and copy the **`client_email`** value (looks like `wipo-tracker@your-project.iam.gserviceaccount.com`).

---

## Step 3 — Create your Google Sheet

1. Open <https://sheets.google.com> → blank sheet. Name it whatever (e.g. "WIPO refusals — LATAM").
2. Click **Share** → paste the service account email from step 2.5 → give it **Editor** access → **Send**.
3. Copy the sheet ID from the URL. It's the long string between `/d/` and `/edit`:
   ```
   https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMn...XYZ/edit
                                          ^^^^^^^^^^^^^^^^^^^^^^
                                          this is GOOGLE_SHEET_ID
   ```

---

## Step 4 — Deploy to Render

1. Go to <https://render.com> and sign up (GitHub login is easiest).
2. Dashboard → **New +** → **Web Service**.
3. Connect your GitHub and pick the repo you pushed in step 1.
4. Render will detect `render.yaml` and pre-fill most fields. Confirm:
   - **Name**: `wipo-refusal-tracker` (or anything)
   - **Runtime**: Python
   - **Plan**: Free
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `gunicorn server:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120`
5. Before hitting **Deploy**, click **Advanced** → **Add Environment Variable** and add these:

   | Key | Value |
   |---|---|
   | `GOOGLE_SHEET_ID` | The long ID you copied in step 3 |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | Paste the **entire contents** of the JSON file from step 2.4. Include all `{ ... }`. |
   | `GOOGLE_SHEET_TAB` | `Refusals` |
   | `WIPO_SAMPLE_MODE` | `0` (set to `1` if you want to see the UI with fake data first) |

6. Hit **Create Web Service**. First build takes ~3 minutes.
7. When it's live, Render gives you a URL like `https://wipo-refusal-tracker.onrender.com`. Open it.

> **Free tier note**: Render's free web services spin down after ~15 min of inactivity. First request after idle takes ~30 seconds to wake up. That's fine for an on-demand lead tool.

---

## Step 5 — Use it

1. Open your Render URL in a browser.
2. Choose how many days back to fetch (7 is a good default for the weekly Gazette).
3. Leave all 10 LATAM countries checked, or narrow to just the ones you service.
4. Click **Fetch from WIPO**. It pulls and parses the daily ROMARIN XMLs.
5. Review the table. Each row shows the mark, the designated country, the holder, and the representative's name + email (that's your lead).
6. Click **Push to Google Sheets** to append everything to your sheet (de-duped automatically).
7. Or click **Download CSV** to import into your own CRM.

Each row in the table also has a ✉ **Email** button that pre-fills a draft in your mail client: subject, greeting, IR number, refusal type, country — all filled in so you can send in 10 seconds.

---

## Adding a schedule later (optional)

When you're ready to have it run automatically:

1. In Render, create a new **Cron Job** service from the same repo.
2. Use schedule `0 10 * * 5` (Fridays at 10:00 UTC — the day after the Gazette publishes).
3. Set the command to:
   ```bash
   python -c "from parser import fetch_latest_refusals; from sheets import push_to_sheet; import os; rows, _ = fetch_latest_refusals(days=7); push_to_sheet(os.environ['GOOGLE_SHEET_ID'], rows)"
   ```
4. Give it the same env vars as the web service.

That will append fresh leads to your sheet every Friday without you clicking a thing.

---

## Running locally (optional)

```bash
pip install -r requirements.txt
export GOOGLE_SHEET_ID=...
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat path/to/service-account.json)"
export WIPO_SAMPLE_MODE=1   # optional — preview UI without WIPO access
python server.py
# open http://localhost:5000
```

---

## Troubleshooting

**"FTP to ftpird.wipo.int failed"**
Some networks block outbound FTP (port 21). Render's free tier allows it. If you're running locally on a corporate network, try from a different connection, or set `WIPO_SAMPLE_MODE=1` to preview the UI.

**"Could not open sheet"**
Make sure you shared the sheet with the service account's `client_email` address (step 3.2). The app impersonates the service account, not you.

**"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON"**
When pasting into Render's env var field, include the entire JSON including the curly braces. Render handles newlines inside the private key correctly as long as you paste the file contents verbatim.

**No refusals found for a given week**
The Gazette publishes on Thursdays. If you're running this on a Monday and chose `days=1`, you'll see nothing — try `days=7`.

---

## What's inside

```
wipo-refusal-tracker/
├── server.py              Flask app with routes
├── parser.py              WIPO FTP fetch + ROMARIN XML parser
├── sheets.py              Google Sheets append with de-dup
├── templates/index.html   Single-page UI
├── requirements.txt       Python deps
├── render.yaml            Render service config
├── Procfile               Fallback process definition
├── runtime.txt            Python version pin
├── .gitignore
└── README.md              You are here
```
