# World Cup Final — 5-in-a-section ticket watcher

Checks resale markets every 30 minutes for the cheapest way to get **5 tickets in
one section** (rows needn't be adjacent — see modes below), and **emails you** when
an option first appears or the price drops. All prices are **all-in (incl. fees)**
so the markets are comparable.

**Event** = FIFA World Cup 2026 **Final** (*Match 104*),
MetLife Stadium, East Rutherford NJ, **Sun Jul 19 2026, 3:00pm ET**.

> Retargeting: event URLs + params live in `check.py` / the workflow. To watch a
> different match, change `STUBHUB_URL` / `VIVID_URL` / `VIAGOGO_URL`, `QUANTITY`,
> `GROUP`, and `MAX_PRICE`.

## Sources
| Source | How | Notes |
|---|---|---|
| **StubHub** | plain HTTP, zero deps | Reads listing data embedded in the page. |
| **viagogo** | plain HTTP, zero deps | StubHub's sibling platform (same event id/format) — often prices the *same* listing differently, so it's useful for cross‑market comparison. |
| **Vivid Seats** | headless browser (`playwright`) | Full inventory (800+ listings); passes the site's bot‑challenge and reads its listings API. |

> SeatGeek (HTTP 403) and TickPick (Cloudflare) hard‑block automated requests, so
> they're not included. This watches **resale/above face value** only — not FIFA's
> official face‑value marketplace (that needs a logged‑in FIFA account).

State is kept in `state.json` so you're emailed on a **first appearance** or **price
drop**, not 48× a day.

## Grouping modes (`GROUP`)
- **`section`** (default): assemble `QUANTITY` seats within a **single section**,
  possibly across several listings — rows needn't be adjacent. Each listing must
  still be at/below `MAX_PRICE`. Good when whole blocks of N-together are scarce.
- **`together`**: a single listing selling `QUANTITY` seats **seated together**.

`MAX_PRICE` is a **per-ticket ceiling** (each listing in a section package must be
at/below it); leave blank for no ceiling. Current config: **5 in a section, ≤ $8,500/tk**
— the Final's floor was ~**$7,500/tk** as of Jul 16.

---

## Option A — Cloud (recommended: runs while your Mac is off)

Free on GitHub Actions, every 30 min.

1. **Push this folder to a repo** (already git‑committed locally for you):
   ```bash
   cd wc-ticket-watch
   # create an empty repo at github.com/new named "wc-ticket-watch", then:
   git remote add origin https://github.com/<you>/wc-ticket-watch.git
   git push -u origin main
   ```
   (Or with the GitHub CLI: `gh repo create wc-ticket-watch --private --source=. --push`.)
2. **Gmail App Password** — on the **sending** account `agentcodymann@gmail.com`,
   turn on 2‑Step Verification, then create one at
   https://myaccount.google.com/apppasswords
3. In the repo → **Settings → Secrets and variables → Actions**:
   - **Secrets** → New repository secret:
     - `SMTP_USER` = `agentcodymann@gmail.com`  *(sender)*
     - `SMTP_PASS` = the 16‑char app password for that account
   - **Variables** → New repository variable:
     - `EMAIL_TO` = `armandomann@gmail.com`  *(where alerts land)*
     - *(optional)* `MAX_PRICE`, `QUANTITY` (default 6), `SOURCES` (default
       `stubhub,vividseats`; set to `stubhub` if Vivid gets challenged on CI).
4. **Actions** tab → enable workflows → **Run workflow** once to test. It then
   self‑runs every 30 min. Delete the repo (or disable the workflow) after Jul 7.

> ⚠️ StubHub *may* block GitHub's datacenter IPs, and Vivid's browser source *may*
> be challenged harder there. If checks stop, the script emails you after ~3 straight
> failures — switch to Option B, or set `SOURCES=stubhub`.

### If GitHub's scheduler won't start (external pinger)

GitHub often doesn't begin firing a **new** repo's `schedule:` for several hours,
and drops cron under load. To trigger runs reliably from outside GitHub, have a
free cron service call the workflow-dispatch API every 30 min:

1. **Create a fine-grained token** at
   <https://github.com/settings/personal-access-tokens/new> →
   *Resource owner* = you, *Repository access* = **Only select repositories →
   `wc-ticket-watch`**, *Permissions → Repository → Actions* = **Read and write**.
   Set expiration past Jul 7. Copy the `github_pat_…` value.
2. **Create a cron job** at <https://cron-job.org> (free) that runs **every 30 min**:
   - **URL** `https://api.github.com/repos/<you>/wc-ticket-watch/actions/workflows/watch.yml/dispatches`
   - **Method** `POST`
   - **Headers**:
     - `Authorization: Bearer github_pat_…`
     - `Accept: application/vnd.github+json`
     - `X-GitHub-Api-Version: 2022-11-28`
   - **Body** `{"ref":"main"}`
3. Save/enable, then hit **Run now** once — a run should appear in the Actions tab
   within seconds. This runs in the cloud regardless of whether your Mac is on.

A `204 No Content` response from the API means success. Revoke the token after Jul 7.

## Option B — Local on your Mac (most reliable IP, only runs while awake)

1. `cp config.example.env config.env` and fill in your Gmail app password.
2. Install the browser for the Vivid source (one time):
   ```bash
   pip3 install playwright && python3 -m playwright install chromium
   ```
3. Test once: `./run-local.sh`
4. Schedule every 30 min with launchd:
   ```bash
   cp com.wc.ticketwatch.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.wc.ticketwatch.plist
   ```
   Stop when done: `launchctl unload ~/Library/LaunchAgents/com.wc.ticketwatch.plist`

## Run by hand anytime
```bash
python3 check.py                       # both sources, cheapest 6-together now
SOURCES=stubhub python3 check.py       # StubHub only (no browser needed)
MAX_PRICE=1800 ./run-local.sh          # only alert if 6-together <= $1,800/ticket
```
