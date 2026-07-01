# World Cup Match 95 — 6-together ticket watcher

Checks resale markets every 30 minutes for the cheapest listing that can sell
**6 tickets seated together**, and **emails you** when they first appear or the
price drops. All prices are **all-in (incl. fees)** so the markets are comparable.

**Match 95** = Round of 16, *Winner Match 86 vs Winner Match 88*,
Mercedes‑Benz Stadium, Atlanta, **Tue Jul 7 2026, 12:00pm ET**.

## Sources
| Source | How | Notes |
|---|---|---|
| **StubHub** | plain HTTP, zero deps | Reads listing data embedded in the page. |
| **Vivid Seats** | headless browser (`playwright`) | Full inventory (800+ listings); passes the site's bot‑challenge and reads its listings API. |

> SeatGeek (HTTP 403) and TickPick (Cloudflare) hard‑block automated requests, so
> they're not included. This watches **resale/above face value** only — not FIFA's
> official face‑value marketplace (that needs a logged‑in FIFA account).

It only counts listings that will actually sell you **6 in one block** — the cheapest
*ticket* on a page often only sells in 2s. State is kept in `state.json` so you're
emailed on a **first appearance** or **price drop**, not 48× a day.

## Alert modes
- **Default** (no `MAX_PRICE`): email when 6‑together first appears and on every drop.
- **Deal mode** (`MAX_PRICE=2000`): stay silent and only email when 6‑together is at
  or below **$2,000/ticket**. Market is ~**$2,270/ticket** all‑in as of Jun 30.

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
2. **Gmail App Password** — turn on 2‑Step Verification, then create one at
   https://myaccount.google.com/apppasswords
3. In the repo → **Settings → Secrets and variables → Actions**:
   - **Secrets** → New repository secret:
     - `SMTP_USER` = `armandomann@gmail.com`
     - `SMTP_PASS` = the 16‑char app password
   - **Variables** → New repository variable:
     - `EMAIL_TO` = `armandomann@gmail.com`
     - *(optional)* `MAX_PRICE`, `QUANTITY` (default 6), `SOURCES` (default
       `stubhub,vividseats`; set to `stubhub` if Vivid gets challenged on CI).
4. **Actions** tab → enable workflows → **Run workflow** once to test. It then
   self‑runs every 30 min. Delete the repo (or disable the workflow) after Jul 7.

> ⚠️ StubHub *may* block GitHub's datacenter IPs, and Vivid's browser source *may*
> be challenged harder there. If checks stop, the script emails you after ~3 straight
> failures — switch to Option B, or set `SOURCES=stubhub`.

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
