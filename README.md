# Red Deer Drop-In Schedule · Auto-Deploy Setup

Scrapes Red Deer City's looknbook for Swim / Skate / Climb and auto-publishes
a bilingual dashboard with conflict detection to Firebase Hosting.

- **Source of truth:** https://looknbook.reddeer.ca
- **Refresh cadence:** daily at ~07:05 Mountain Time
- **Cost:** $0 (GitHub Actions free tier + Firebase Hosting free tier)
- **Conflict logic:** flags cross-facility sessions within ±30 min of each other

---

## Repo Layout

```
your-repo/
├── .github/workflows/
│   └── refresh-schedule.yml   # cron workflow
├── scripts/
│   └── sync.py                # scraper + renderer + conflict detector
├── public/
│   └── index.html             # generated dashboard (auto-committed)
├── firebase.json              # Hosting config
├── .firebaserc                # Firebase project link
└── requirements.txt
```

---

## One-Time Setup (your current state: Firebase+GitHub already linked)

### Step 1 — Drop the files in place

From the provided bundle, copy:

| From                         | To in your repo                              |
| ---------------------------- | -------------------------------------------- |
| `sync.py`                    | `scripts/sync.py`                            |
| `workflow_refresh-schedule.yml` | `.github/workflows/refresh-schedule.yml`  |
| `firebase.json`              | `firebase.json`  (merge if you already have one — see below) |
| `firebaserc.json`            | `.firebaserc`    (rename; skip if you already have one) |
| `requirements.txt`           | `requirements.txt` (optional)                |

**If `firebase.json` already exists in your repo:** keep your existing
`firebase.json` but ensure `hosting.public` is set to `"public"`. Then place
`index.html` inside `public/`. No other merging required.

**If `.firebaserc` already exists:** leave it alone.

### Step 2 — Verify the Firebase ↔ GitHub link

If you deployed via Firebase Hosting's GitHub integration
(`firebase init hosting` → "Set up automatic builds with GitHub"), there's
already a workflow file like `.github/workflows/firebase-hosting-merge.yml`
in your repo. That workflow redeploys on every push to `main`.

👉 **Nothing to change — our new workflow commits to `main`, which triggers
your existing deploy workflow. The two workflows cooperate.**

### Step 3 — First manual run

```bash
# Local sanity check (optional but recommended)
pip install -r requirements.txt
python scripts/sync.py --out public/index.html
firebase deploy --only hosting    # uses your existing CLI login
```

Open the printed Hosting URL. Confirm the dashboard renders.

### Step 4 — Enable the scheduled workflow

In GitHub:

1. Commit and push all files to `main`.
2. Go to **Actions** tab → you should see *Refresh Schedule* listed.
3. Click **Run workflow** (the manual dispatch button) to verify it works
   end-to-end before waiting for the cron.
4. After success, the daily cron takes over automatically.

---

## How It Works

```
┌────────────────────────┐
│ GitHub Actions (13:05  │
│ UTC daily cron)        │
└───────┬────────────────┘
        │
        ▼
┌────────────────────────┐
│ python scripts/sync.py │  ← fetches 42 pages (3 cats × 14 days) in parallel
│   → public/index.html  │  ← renders dashboard + conflict badges
└───────┬────────────────┘
        │
        ▼
┌────────────────────────┐
│ git commit + push main │  ← only if file changed
└───────┬────────────────┘
        │
        ▼
┌────────────────────────┐
│ Firebase Hosting       │  ← auto-deploys via existing github integration
│ .web.app URL           │
└────────────────────────┘
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Workflow fails with `403` on `git push` | Default token has read-only | `permissions: contents: write` is already in the workflow — confirm repo Settings → Actions → Workflow permissions = "Read and write" |
| Workflow runs but Hosting doesn't update | Firebase GitHub integration disabled | Check `.github/workflows/firebase-hosting-*.yml` exists and hasn't been disabled |
| `No classes within that range` for climbing | Climbing is seasonal (likely Oct–Apr) | Not a bug; the row will show "— no sessions —" until programs return |
| Schedule looks stale for today but has tomorrow | UTC-cron fired before Red Deer midnight | Expected on rare edge days; next-day catch-up fixes automatically |
| GUIDs stop returning data | City rotated CategoryGUIDs | Re-harvest from `https://looknbook.reddeer.ca/RedDeer/public/category/browse/DROPINSWIM` (etc.) and update top of `sync.py` |

---

## Optional Hardening

- **Commit only on content change, not timestamp:** current workflow commits on any diff, which means a daily commit even if the schedule is identical (timestamp in HTML differs). To suppress heartbeat commits, store data as JSON and diff that instead — ask me to refactor if you want this.
- **Email/Slack on conflicts:** add a step that greps `class="badge conflict"` count; if >0, send a notification.
- **iCal export:** generate `public/schedule.ics` so preferred windows sync to your phone calendar.

Confidence: **High** on scraping + rendering + conflict logic (all tested).
Confidence: **Mid** on first-push activation — may need the Settings → Actions
permissions toggle if your repo has the older default.
