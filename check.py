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

# Match 95 = Round of 16, W86 vs W88, Mercedes-Benz Stadium Atlanta, Jul 7 2026.
STUBHUB_URL = env(
    "STUBHUB_URL",
    "https://www.stubhub.com/world-cup-atlanta-tickets-7-7-2026/event/155049347/").strip()
VIVID_URL = env(
    "VIVID_URL",
    "https://www.vividseats.com/world-cup-soccer-tickets-mercedes-benz-stadium-7-7-2026--sports-soccer/production/5080860").strip()

SOURCES = [s.strip() for s in env("SOURCES", "stubhub,vividseats").split(",") if s.strip()]
QUANTITY = int(env("QUANTITY", "6"))

# Optional target. When set, the watcher goes QUIET and only emails when a
# 6-together offer is at/below this per-ticket price (a "tell me when it's a deal"
# mode). When unset, it emails on first appearance and every price drop.
MAX_PRICE = env("MAX_PRICE")
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


def source_stubhub():
    """Return (offers, error). StubHub prices are already all-in (incl. fees)."""
    req = urllib.request.Request(STUBHUB_URL, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return [], f"fetch failed: {e}"

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

    offers = []
    for o in seen.values():
        aq = o.get("availableQuantities") or []
        if QUANTITY in aq and o.get("isSeatedTogether", False):
            lid = o.get("listingId")
            sep = "&" if "?" in STUBHUB_URL else "?"
            url = f"{STUBHUB_URL}{sep}quantity={QUANTITY}"
            if lid is not None:
                url += f"&listingId={lid}"
            offers.append({
                "source": "StubHub",
                "price": float(o["rawPrice"]),
                "qty": o.get("availableTickets"),
                "section": o.get("section"),
                "row": o.get("row"),
                "listing_id": lid,
                "url": url,
            })
    return offers, None


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

    offers = []
    for o in body["tickets"]:
        try:
            q = int(o.get("quantity") or 0)
        except (TypeError, ValueError):
            q = 0
        together = "Seated Together" in (o.get("perks") or [])
        if together and buyable(q, QUANTITY):
            price = o.get("allInPricePerTicket") or o.get("aip")
            if price is None:
                continue
            lid = o.get("id") or o.get("listingId")
            sep = "&" if "?" in VIVID_URL else "?"
            offers.append({
                "source": "Vivid Seats",
                "price": float(price),
                "qty": q,
                "section": o.get("sectionName"),
                "row": o.get("row"),
                "listing_id": lid,
                "url": f"{VIVID_URL}{sep}qty={QUANTITY}",
            })
    return offers, None


SOURCE_FUNCS = {"stubhub": source_stubhub, "vividseats": source_vividseats}


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
    return f"{o['source']}:{o.get('section')}|{o.get('row')}|{o.get('qty')}"


def seat_sort_key(o):
    """Group by seat location so the same tickets listed on different
    marketplaces sit next to each other; cheapest first as the tiebreak."""
    return (str(o.get("section") or ""), str(o.get("row") or ""),
            o.get("qty") or 0, o["price"])


def fmt_offer(o):
    total = o["price"] * QUANTITY
    return (f"  Sec {o.get('section') or '?'} · Row {o.get('row') or '?'} · "
            f"{o.get('qty')} seats · ${o['price']:,.0f}/tk · {o['source']}"
            f"  (${total:,.0f} for {QUANTITY})\n"
            f"    {o['url']}")


def fmt_change(o, old):
    arrow = "🔻" if o["price"] < old else "🔺"
    total = o["price"] * QUANTITY
    return (f"  Sec {o.get('section') or '?'} · Row {o.get('row') or '?'} · "
            f"{o.get('qty')} seats · {arrow} ${old:,.0f} → ${o['price']:,.0f}/tk · {o['source']}"
            f"  (${total:,.0f} for {QUANTITY})\n"
            f"    {o['url']}")


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
    print(f"=== {now}  qty={QUANTITY}  sources={SOURCES}"
          + (f"  target=${MAX_PRICE:,.0f}/tk" if MAX_PRICE else ""))

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
        print(f"[{name}] {len(offers)} offers of {QUANTITY} seated together")
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

    by_src = {}
    for o in sorted(all_offers, key=lambda o: o["price"]):
        by_src.setdefault(o["source"], o["price"])
    summary = ", ".join(f"{s} ${p:,.0f}" for s, p in sorted(by_src.items(), key=lambda x: x[1]))

    # Viable = seated-together listings at/below the target (all of them if no
    # target), sorted cheapest first.
    viable = sorted(
        (o for o in all_offers if MAX_PRICE is None or o["price"] <= MAX_PRICE),
        key=lambda o: o["price"])
    cap = f" under ${MAX_PRICE:,.0f}/tk" if MAX_PRICE is not None else ""
    print(f"[result] {len(viable)} viable{cap}"
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
        subject = (f"⚽ Match 95: {', '.join(counts)} — "
                   f"best ${cheapest['price']:,.0f}/tk ({cheapest['source']})")

        lines = [f"World Cup Match 95 (Round of 16, Atlanta, Jul 7) — "
                 f"{QUANTITY} seated together, all-in{cap}",
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
