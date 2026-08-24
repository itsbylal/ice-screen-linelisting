# ICE TB Programme — Xpert MTB/RIF Line List

Interactive line list for the ICE TB active case-finding programme.
Mercy Corps Pakistan.

2,996+ records across 162 variables, sourced from the CommCare → Synapse export
in Azure Blob Storage.

---

## ⚠️ Read this before you commit anything

This repository must **never** contain:

| Never commit | Why |
|---|---|
| `source_url.txt` | Holds the Azure SAS token — a working read credential for the full dataset |
| `snapshots/` | Patient-level records including `PI_FullName` |
| `ice_tb_line_list.html` | The **local** build embeds both the records and the SAS token |
| Exported CSVs | Same patient data |

`.gitignore` already excludes all of these, and the deploy workflow fails the
build if a SAS token or embedded records are detected in `www/` or `api/`.
Do not use `git add -f` on any of them.

If a token is ever committed: rotate it in Azure first (a force-push does not
remove it from clones or forks), then clean history.

---

## How it fits together

```
CommCare  →  Synapse  →  Azure Blob (ice_cumulative_data.json)
                                    │
                    ┌───────────────┴────────────────┐
                    │                                │
        ICE_SOURCE app setting              source_url.txt (local only)
                    │                                │
            /api/data  (Node function)        build_linelist.py
            authenticated proxy                      │
                    │                                │
            www/index.html                  ice_tb_line_list.html
        (no data, no token — safe)      (data + token embedded — never commit)
                    │                                │
        Static Web App + Entra ID              single local file
        colleagues sign in                     for offline use
```

Two builds from one template:

| | Hosted (`www/index.html`) | Local (`ice_tb_line_list.html`) |
|---|---|---|
| Size | ~51 KB | ~3.4 MB |
| Records embedded | none — fetched after sign-in | all, at build time |
| SAS token | server-side only | inside the file |
| Access control | Entra ID sign-in | password gate (deterrent only) |
| Works offline | no | yes |
| Safe to commit | **yes** | **no** |

---

## Deploying the hosted version

### 1. Push this repo (private)

Create the repository on GitHub with **Add README, Add .gitignore and Add
license all left off** — any of them creates a commit on the remote and your
first push is then rejected as non-fast-forward.

```bash
git init
git config core.hooksPath hooks     # activates the pre-push safety check
git add .
git commit -m "ICE TB line list"
git branch -M main
git remote add origin https://github.com/<org>/ice-tb-line-list.git
git push -u origin main
```

The `core.hooksPath` line matters. GitHub's own push protection does **not**
cover private repositories on personal accounts, so `hooks/pre-push` is the
local stand-in: it refuses the push if a SAS token, embedded records,
`source_url.txt`, `snapshots/` or `ice_tb_line_list.html` are tracked.
It is per-clone configuration, so every collaborator must run it once.

Sanity check before the first push:

```bash
git config core.hooksPath        # -> hooks
git ls-files                     # should list 14 files, no data, no token
```

### 2. Create the Static Web App

Azure portal → **Create a resource** → Static Web App:

- Plan: **Free**
- Deployment source: **GitHub**, pick this repo and the `main` branch
- Build presets: **Custom** — app location `www`, api location `api`,
  output location blank

Azure adds `AZURE_STATIC_WEB_APPS_API_TOKEN` to the repo secrets and commits a
workflow of its own. Delete the workflow Azure generates and keep
`.github/workflows/deploy.yml` from this repo — it carries the pre-deploy
secret/data checks.

### 3. Register the app in Entra ID

Entra ID → App registrations → New registration:

- Supported account types: **Single tenant** — this is what stops anyone
  outside Mercy Corps from signing in
- Redirect URI (Web): `https://<your-site>.azurestaticapps.net/.auth/login/aad/callback`
- Then Certificates & secrets → New client secret → copy the value

### 4. Wire up the settings

Static Web App → **Environment variables** (Configuration), add:

| Name | Value |
|---|---|
| `ICE_SOURCE` | the full Azure Blob SAS URL |
| `AAD_CLIENT_ID` | Application (client) ID from step 3 |
| `AAD_CLIENT_SECRET` | the client secret value from step 3 |

Then edit `staticwebapp.config.json` and replace `PASTE_TENANT_ID` with your
Directory (tenant) ID. Commit and push — that triggers a redeploy.

### 5. Give colleagues access

By default any account in your tenant can sign in. To narrow it further,
set the Entra ID app registration to **Assignment required** and add only the
people or group who should see the line list.

Send them the `https://<your-site>.azurestaticapps.net` URL. They sign in with
their work account; no password to share, and access is revoked automatically
when someone leaves.

---

## Running the local build

For offline use or when you want a single file to hand over.

```bash
# once: put the SAS URL in source_url.txt (never committed)
python build/build_linelist.py                 # → ice_tb_line_list.html
python build/build_linelist.py --hosted        # → www/index.html
python build/build_linelist.py path/to.json    # rebuild from a saved snapshot
```

`refresh_line_list.bat` does the same on Windows and is what you point Task
Scheduler at for a daily 07:00 refresh.

Safety behaviour: a failed download, a truncated file or the wrong file at that
URL all abort and leave the previous page intact. Every good fetch is archived
to `snapshots/`. The SAS expiry is logged on each run and warns from 30 days out.

The local build's password gate is `icetb@786`. Be clear-eyed about what it is:
it stops casual viewing, but the records sit in the file in plain text and
anyone can read them in a text editor without the password. It is not a
substitute for the hosted version.

---

## Data notes

Decoded at build time, and identically in the browser for live loads:

- `MTB` — 1 Detected, 2 Not Detected, 3 Invalid, 4 Error
- `MTBDetected` — 1 High, 2 Medium, 3 Low, 4 Very Low, 5 Trace
- `RR` — 1 Detected, 2 Not Detected, 3 Indeterminate
- `XpertPerformed` — 1 Yes, 2 No
- `PI_Gender` — 1 Male, 2 Female, 3 Other
- `CP_Province`, `LaboratoryName` — slugs mapped to readable names

Other numeric-coded fields (`VS_*`, `RF_*`, sample appropriateness, and so on)
are shown as raw codes. Supply a codebook and they can be decoded too.

Known data-quality items: 132 records have a blank `UID`; two `UID`s are
8 digits against the 6-digit norm; `Refusal_Reasons` is free text with
inconsistent capitalisation and needs cleaning before it can be counted.

---

## Repository layout

```
├── www/index.html          hosted dashboard (no data, no token)
├── api/data/               authenticated proxy to the blob
├── build/
│   ├── build_linelist.py   builds either variant
│   ├── linelist_template.html
│   ├── schema_header.csv   canonical 162-column order
│   └── refresh_line_list.bat
├── staticwebapp.config.json  auth + routing
├── hooks/pre-push          blocks pushing tokens or patient data
├── .github/workflows/deploy.yml
└── .gitignore
```

Edit the dashboard in `build/linelist_template.html`, never in the generated
files — a rebuild overwrites them.
