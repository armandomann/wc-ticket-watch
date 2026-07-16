#!/usr/bin/env python3
"""
World Cup 2026 ticket watcher — multi-source.

Finds the cheapest offer of N tickets seated together (default 6) across resale
markets and emails you when they first appear or the price drops. Prices are
all-in (incl. fees) so sources are comparable.

Sources:
  * stubhub    — plain HTTP, zero dependencies. Reads listings embedded in the page.
  * vividseats — full inventory via a headless browser (needs `playwright`), gets
                 past the site's bot-challenge and captures its listings API.

Enable sources with the SOURCES env var (e.g. "stubhub,vividseats"). If playwright
isn't installed, the vividseats source is skipped with a warning.

Config is via environment variables — see config.example.env.
"""

import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage

# ---- Config -----------------------------------------------------------------

def env(name, default=""):
    """Env var value, falling back to `default` when unset OR empty.

    GitHub Actions passes `${{ vars.X }}` as an empty string when the variable
    isn't set, so a plain os.environ.get(name, default) would return "" and
    clobber the default. Treat empty the same as unset.
    """
    v = os.environ.get(name)
    return v if v not in (None, "") else default

# Final = Match 104, MetLife Stadium (East Rutherford, NJ), Sun Jul 19 2026.
STUBHUB_URL = env(
    "STUBHUB_URL",
    "https://www.stubhub.com/world-cup-east-rutherford-tickets-7-19-2026/event/153020449/").strip()
VIVID_URL = env(
    "VIVID_URL",
    "https://www.vividseats.com/world-cup-soccer-tickets-hard-rock-stadium-7-19-2026--sports-soccer/production/5080877").strip()
# viagogo is StubHub's sibling platform (same event id, same embedded data
# format) but often prices differently — good for cross-market comparison.
VIAGOGO_URL = env(
    "VIAGOGO_URL",
    "https://www.viagogo.com/Sports-Tickets/Soccer/Soccer-Tournament/World-Cup-Tickets/E-153020449").strip()

# Short tag + full description used in email subject/body.
EVENT_TAG = env("EVENT_TAG", "WC Final")
EVENT_LABEL = env("EVENT_LABEL", "World Cup Final (Match 104, MetLife Stadium, Jul 19)")

SOURCES = [s.strip() for s in env("SOURCES", "stubhub,viagogo,vividseats").split(",") if s.strip()]
QUANTITY = int(env("QUANTITY", "5"))

# Grouping mode:
#   together — one listing must sell QUANTITY seats seated together (original).
#   section  — QUANTITY seats assembled within a single section, possibly across
#              several listings (rows needn't be adjacent). Each component listing
#              must still be at/below MAX_PRICE.
GROUP = env("GROUP", "section").strip().lower()

# StubHub/viagogo embed only a partial, rotating subset of listings per request,
# so we fetch a few times and union by listingId to cover the inventory. Passes
# stop early once two in a row add nothing new.
SH_PASSES = int(env("SH_PASSES", "8"))
SH_PASS_DELAY = float(env("SH_PASS_DELAY", "1.5"))  # seconds between passes

# Per-ticket price ceiling. Only listings at/below this count. In section mode
# every component of a section package must be at/below it. Set MAX_PRICE="" to
# disable (then every listing counts). Defaults to $8k/tk for the Final.
MAX_PRICE = env("MAX_PRICE", "8000")
MAX_PRICE = float(MAX_PRICE) if MAX_PRICE else None

# Ignore price wobbles smaller than this ($/ticket) so trivial fluctuations
# don't trigger a "price change" email every run. Raise it if StubHub is noisy.
PRICE_CHANGE_MIN = float(env("PRICE_CHANGE_MIN", "25"))

STATE_FILE = env("STATE_FILE", "state.json")

SMTP_HOST = env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(env("SMTP_PORT", "587"))
SMTP_USER = env("SMTP_USER").strip()
# Gmail shows app passwords as 4 space-separated groups ("abcd efgh ijkl mnop");
# they must be sent with no spaces, so strip all whitespace defensively.
SMTP_PASS = "".join(env("SMTP_PASS").split())
EMAIL_TO = env("EMAIL_TO", SMTP_USER).strip()

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def buyable(total_qty, want):
    """Standard 'no single seat left behind' rule: you can buy `want` from a
    listing of `total_qty` if it's an exact match or leaves 2+ behind."""
    return total_qty == want or (total_qty > want and total_qty - want >= 2)


# ---- Source: StubHub (plain HTTP) -------------------------------------------

def _enclosing_object(s, pos):
    depth = 0
    start = None
    for i in range(pos, -1, -1):
        c = s[i]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                start = i
                break
            depth -= 1
    if start is None:
        return None
    depth = 0
    for j in range(start, len(s)):
        c = s[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start:j + 1]
    return None


def _sh_fetch_once(page_url):
    """One fetch of a StubHub/viagogo page, returning {listingId: obj} for every
    embedded listing (or raising on a network/HTTP error). The `?quantity=N`
    filter biases the embedded set toward listings that can sell N together."""
    sep = "&" if "?" in page_url else "?"
    fetch_url = f"{page_url}{sep}quantity={QUANTITY}"
    req = urllib.request.Request(fetch_url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", "replace")
    seen = {}
    for m in re.finditer(r'"rawPrice"', html):
        txt = _enclosing_object(html, m.start())
        if not txt:
            continue
        try:
            o = json.loads(txt)
        except ValueError:
            continue
        if o.get("listingId") is not None and "rawPrice" in o:
            seen[o["listingId"]] = o
    return seen


def _scrape_sh_platform(page_url, source_name):
    """StubHub and viagogo share one platform: listing data is embedded in the
    page as JSON objects with rawPrice / listingId / isSeatedTogether /
    availableQuantities. Prices are already all-in (incl. fees).

    Each request only embeds a partial, rotating subset of the inventory, and
    the (sparse) N-together listings only rotate in every few requests, so we
    fetch SH_PASSES times and union by listingId to cover them. Returns
    (offers, error)."""
    seen = {}
    last_err = None
    errs = 0
    fetched_ok = False
    for i in range(SH_PASSES):
        if i:
            time.sleep(SH_PASS_DELAY)
        try:
            batch = _sh_fetch_once(page_url)
        except Exception as e:  # noqa: BLE001
            last_err = f"fetch failed: {e}"
            errs += 1
            if errs >= 2:  # persistent block (e.g. 403) — retrying won't help
                break
            continue
        fetched_ok = True
        errs = 0
        seen.update(batch)
    if not fetched_ok:
        return [], last_err or "fetch failed"

    # Emit every listing (normalized); main() applies the grouping-mode filter.
    listings = []
    for o in seen.values():
        try:
            price = float(o["rawPrice"])
        except (TypeError, ValueError, KeyError):
            continue
        aq = o.get("availableQuantities") or []
        try:
            avail = int(o.get("availableTickets") or (max(aq) if aq else 0))
        except (TypeError, ValueError):
            avail = 0
        lid = o.get("listingId")
        sep = "&" if "?" in page_url else "?"
        url = f"{page_url}{sep}quantity={QUANTITY}"
        if lid is not None:
            url += f"&listingId={lid}"
        listings.append({
            "source": source_name,
            "price": price,
            "qty": avail,
            "avail": avail,
            "avail_qtys": aq,
            "together": bool(o.get("isSeatedTogether", False)),
            "section": o.get("section"),
            "row": o.get("row"),
            "listing_id": lid,
            "url": url,
        })
    return listings, None


def source_stubhub():
    return _scrape_sh_platform(STUBHUB_URL, "StubHub")


def source_viagogo():
    return _scrape_sh_platform(VIAGOGO_URL, "viagogo")


# ---- Source: Vivid Seats (headless browser) ---------------------------------

def source_vividseats():
    """Return (offers, error). Uses playwright to pass the bot-challenge and
    capture the listings API. Prices are all-in (allInPricePerTicket)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], "playwright not installed (pip install playwright && playwright install chromium)"

    payload = {}
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            ctx = b.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
            pg = ctx.new_page()

            def on_resp(r):
                if "hermes/api/v1/listings?productionId=" in r.url:
                    try:
                        payload["body"] = r.json()
                    except Exception:  # noqa: BLE001
                        pass

            pg.on("response", on_resp)
            pg.goto(VIVID_URL, wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(7000)
            b.close()
    except Exception as e:  # noqa: BLE001
        return [], f"browser error: {e}"

    body = payload.get("body")
    if not body or "tickets" not in body:
        return [], "no listings captured (challenge or layout change?)"

    # Emit every listing (normalized); main() applies the grouping-mode filter.
    listings = []
    for o in body["tickets"]:
        try:
            q = int(o.get("quantity") or 0)
        except (TypeError, ValueError):
            q = 0
        price = o.get("allInPricePerTicket") or o.get("aip")
        if price is None:
            continue
        lid = o.get("id") or o.get("listingId")
        sep = "&" if "?" in VIVID_URL else "?"
        listings.append({
            "source": "Vivid Seats",
            "price": float(price),
            "qty": q,
            "avail": q,
            "avail_qtys": None,
            "together": "Seated Together" in (o.get("perks") or []),
            "section": o.get("sectionName"),
            "row": o.get("row"),
            "listing_id": lid,
            "url": f"{VIVID_URL}{sep}qty={QUANTITY}",
        })
    return listings, None


SOURCE_FUNCS = {"stubhub": source_stubhub, "viagogo": source_viagogo,
                "vividseats": source_vividseats}


# ---- State & email ----------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def offer_key(o):
    """Stable identity for a listing across runs — prefer the market's listing
    id, else fall back to section/row/qty."""
    lid = o.get("listing_id")
    if lid is not None:
        return f"{o['source']}:{lid}"
    return (f"{o['source']}:{norm_label(o.get('section'))}|"
            f"{norm_label(o.get('row'))}|{o.get('qty')}")


def norm_label(v):
    """Normalize a section/row label so the same seat reads the same across
    marketplaces: drop 'Section'/'Sec'/'Row' prefixes, punctuation, spaces and
    case, and strip leading zeros ("Section 05" and "sec5" -> "5"). Best-effort
    only — it can't reconcile genuinely different naming schemes."""
    s = str(v or "").strip().lower()
    s = re.sub(r"^(sections?|sec\.?|sect\.?|rows?|rw\.?)\s*", "", s)
    s = re.sub(r"[^a-z0-9]", "", s)
    m = re.match(r"^([a-z]*)0*(\d+)$", s)
    return m.group(1) + m.group(2) if m else s


def disp_label(v):
    """Clean a label for display: drop a redundant leading 'Section'/'Row'
    prefix (we add our own) but otherwise keep the site's original text."""
    s = re.sub(r"^(sections?|sec\.?|sect\.?|rows?|rw\.?)\s*", "", str(v or "").strip(),
               flags=re.I)
    return s or "?"


def seat_sort_key(o):
    """Group by (normalized) seat location so the same tickets listed on
    different marketplaces sit next to each other; cheapest first as the
    tiebreak."""
    return (norm_label(o.get("section")), norm_label(o.get("row")),
            o.get("qty") or 0, o["price"])


def fmt_offer(o):
    if "breakdown" in o:  # section package
        return (f"  Sec {disp_label(o.get('section'))} · {QUANTITY} in section · "
                f"avg ${o['price']:,.0f}/tk · {o['source']}  (${o['total']:,.0f} for {QUANTITY})\n"
                f"    seats: {o['breakdown']}\n"
                f"    {o['url']}")
    total = o["price"] * QUANTITY
    return (f"  Sec {disp_label(o.get('section'))} · Row {disp_label(o.get('row'))} · "
            f"{o.get('qty')} seats · ${o['price']:,.0f}/tk · {o['source']}"
            f"  (${total:,.0f} for {QUANTITY})\n"
            f"    {o['url']}")


def fmt_change(o, old):
    arrow = "🔻" if o["price"] < old else "🔺"
    if "breakdown" in o:  # section package
        return (f"  Sec {disp_label(o.get('section'))} · {QUANTITY} in section · "
                f"{arrow} ${old:,.0f} → ${o['price']:,.0f}/tk avg · {o['source']}\n"
                f"    seats: {o['breakdown']}\n"
                f"    {o['url']}")
    total = o["price"] * QUANTITY
    return (f"  Sec {disp_label(o.get('section'))} · Row {disp_label(o.get('row'))} · "
            f"{o.get('qty')} seats · {arrow} ${old:,.0f} → ${o['price']:,.0f}/tk · {o['source']}"
            f"  (${total:,.0f} for {QUANTITY})\n"
            f"    {o['url']}")


# ---- Grouping: pick the viable offers for the configured mode ---------------

def together_offers(listings):
    """`together` mode: a single listing that can sell QUANTITY seated together,
    at/below MAX_PRICE. Cheapest first."""
    out = []
    for o in listings:
        if MAX_PRICE is not None and o["price"] > MAX_PRICE:
            continue
        aq = o.get("avail_qtys")
        ok = o.get("together") and (
            (aq and QUANTITY in aq) or (not aq and buyable(o.get("avail") or 0, QUANTITY)))
        if ok:
            out.append(o)
    return sorted(out, key=lambda o: o["price"])


def section_packages(listings):
    """`section` mode: assemble QUANTITY seats within one (source, section),
    taking the cheapest seats first from listings at/below MAX_PRICE. Rows need
    not be adjacent. Returns one package "offer" per qualifying section (shaped
    like a listing so the rest of the pipeline is unchanged), cheapest first."""
    from collections import defaultdict
    groups = defaultdict(list)
    for o in listings:
        if MAX_PRICE is not None and o["price"] > MAX_PRICE:
            continue
        if (o.get("avail") or 0) <= 0:
            continue
        groups[(o["source"], norm_label(o.get("section")))].append(o)

    packages = []
    for (source, secnorm), ls in groups.items():
        ls.sort(key=lambda o: o["price"])
        got, cost, parts = 0, 0.0, []
        for o in ls:
            take = min(o["avail"], QUANTITY - got)
            if take <= 0:
                break
            got += take
            cost += take * o["price"]
            parts.append((take, o))
            if got >= QUANTITY:
                break
        if got < QUANTITY:
            continue  # section can't supply QUANTITY under the cap
        head = parts[0][1]
        packages.append({
            "source": source,
            "price": cost / QUANTITY,      # per-ticket average for the package
            "total": cost,
            "qty": QUANTITY,
            "section": head.get("section"),
            "row": f"{len(parts)} listing(s)",
            "breakdown": "; ".join(
                f"{n}×${p['price']:,.0f}/tk R{disp_label(p.get('row'))}" for n, p in parts),
            "listing_id": f"pkg:{secnorm}",   # stable per-section identity
            "url": head.get("url"),
        })
    return sorted(packages, key=lambda o: o["price"])


def send_email(subject, body):
    if not (SMTP_USER and SMTP_PASS and EMAIL_TO):
        print("[email] SMTP not configured; skipping. Would have sent:")
        print(f"[email] Subject: {subject}\n{body}")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg.set_content(body)
    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls(context=ctx)
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    print(f"[email] sent to {EMAIL_TO}: {subject}")
    return True


# ---- Main -------------------------------------------------------------------

def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"=== {now}  qty={QUANTITY}  mode={GROUP}  sources={SOURCES}"
          + (f"  cap=${MAX_PRICE:,.0f}/tk" if MAX_PRICE else ""))

    all_offers = []
    errors = {}
    for name in SOURCES:
        fn = SOURCE_FUNCS.get(name)
        if not fn:
            print(f"[{name}] unknown source, skipping")
            continue
        offers, err = fn()
        if err:
            errors[name] = err
            print(f"[{name}] ERROR: {err}")
        print(f"[{name}] {len(offers)} listings")
        all_offers.extend(offers)

    state = load_state()

    # If every source errored, treat as a fetch failure (alert sparingly).
    if errors and len(errors) == len(SOURCES):
        fails = state.get("consecutive_fetch_failures", 0) + 1
        state["consecutive_fetch_failures"] = fails
        if fails in (3, 20):
            send_email("⚠️ WC ticket watcher: all sources unreachable",
                       "All sources failed:\n" +
                       "\n".join(f"- {k}: {v}" for k, v in errors.items()) +
                       "\n\nCloud IP may be blocked; consider running locally (README).")
        save_state(state)
        sys.exit(1)
    state["consecutive_fetch_failures"] = 0

    # Viable offers for the configured grouping mode, cheapest first.
    if GROUP == "section":
        viable = section_packages(all_offers)
        unit = f"{QUANTITY}-in-section"
    else:
        viable = together_offers(all_offers)
        unit = f"{QUANTITY}-together"

    by_src = {}
    for o in viable:  # cheapest qualifying offer per source
        by_src.setdefault(o["source"], o["price"])
    summary = ", ".join(f"{s} ${p:,.0f}" for s, p in sorted(by_src.items(), key=lambda x: x[1]))

    cap = f" under ${MAX_PRICE:,.0f}/tk" if MAX_PRICE is not None else ""
    print(f"[result] {len(viable)} viable {unit}{cap}"
          + (f"; cheapest ${viable[0]['price']:,.0f}/tk on {viable[0]['source']}  ({summary})"
             if viable else ""))

    # Compare against the listings recorded on the previous run to split into
    # new finds / price changes / unchanged repeats.
    prev_listings = state.get("listings", {})
    new_finds, changes, repeats = [], [], []
    for o in viable:
        rec = prev_listings.get(offer_key(o))
        old = rec.get("price") if isinstance(rec, dict) else None
        if old is None:
            new_finds.append(o)
        elif abs(old - o["price"]) >= PRICE_CHANGE_MIN:
            changes.append((o, old))
        else:
            repeats.append(o)

    # Record this run's viable listings as the baseline for next time.
    state["listings"] = {
        offer_key(o): {"price": o["price"], "source": o["source"],
                       "section": o.get("section"), "row": o.get("row"),
                       "qty": o.get("qty")}
        for o in viable}
    state.update(last_best_price=(viable[0]["price"] if viable else None),
                 last_checked=now, by_source=by_src)

    # Alert only when something is new or a price moved — repeats alone stay quiet.
    if new_finds or changes:
        counts = []
        if new_finds:
            counts.append(f"{len(new_finds)} new")
        if changes:
            counts.append(f"{len(changes)} price change{'s' if len(changes) != 1 else ''}")
        cheapest = viable[0]
        subject = (f"⚽ {EVENT_TAG}: {', '.join(counts)} — "
                   f"best ${cheapest['price']:,.0f}/tk ({cheapest['source']})")

        mode_desc = (f"{QUANTITY} in one section (rows needn't be adjacent)"
                     if GROUP == "section" else f"{QUANTITY} seated together")
        lines = [f"{EVENT_LABEL} — {mode_desc}, all-in{cap}",
                 f"Checked {now}", ""]
        if new_finds:
            lines.append(f"🆕 NEW FINDS ({len(new_finds)})")
            lines += [fmt_offer(o) for o in sorted(new_finds, key=seat_sort_key)]
            lines.append("")
        if changes:
            lines.append(f"🔄 PRICE CHANGES ({len(changes)})")
            lines += [fmt_change(o, old) for o, old
                      in sorted(changes, key=lambda c: seat_sort_key(c[0]))]
            lines.append("")
        if repeats:
            lines.append(f"📋 STILL AVAILABLE ({len(repeats)}) — unchanged since last alert")
            lines += [fmt_offer(o) for o in sorted(repeats, key=seat_sort_key)]
            lines.append("")
        lines.append(f"Cheapest per market: {summary}")
        send_email(subject, "\n".join(lines))
    else:
        print("[email] no new finds or price changes; staying quiet.")

    save_state(state)


if __name__ == "__main__":
    main()
