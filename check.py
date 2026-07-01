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

# Match 95 = Round of 16, W86 vs W88, Mercedes-Benz Stadium Atlanta, Jul 7 2026.
STUBHUB_URL = os.environ.get(
    "STUBHUB_URL",
    "https://www.stubhub.com/world-cup-atlanta-tickets-7-7-2026/event/155049347/").strip()
VIVID_URL = os.environ.get(
    "VIVID_URL",
    "https://www.vividseats.com/world-cup-soccer-tickets-mercedes-benz-stadium-7-7-2026--sports-soccer/production/5080860").strip()

SOURCES = [s.strip() for s in os.environ.get("SOURCES", "stubhub,vividseats").split(",") if s.strip()]
QUANTITY = int(os.environ.get("QUANTITY") or "6")

# Optional target. When set, the watcher goes QUIET and only emails when a
# 6-together offer is at/below this per-ticket price (a "tell me when it's a deal"
# mode). When unset, it emails on first appearance and every price drop.
MAX_PRICE = os.environ.get("MAX_PRICE")
MAX_PRICE = float(MAX_PRICE) if MAX_PRICE not in (None, "") else None

STATE_FILE = os.environ.get("STATE_FILE", "state.json")

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_TO = os.environ.get("EMAIL_TO", SMTP_USER)

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
            offers.append({
                "source": "StubHub",
                "price": float(o["rawPrice"]),
                "qty": o.get("availableTickets"),
                "section": o.get("section"),
                "row": o.get("row"),
                "url": STUBHUB_URL,
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
            offers.append({
                "source": "Vivid Seats",
                "price": float(price),
                "qty": q,
                "section": o.get("sectionName"),
                "row": o.get("row"),
                "url": VIVID_URL,
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

    prev = state.get("last_best_price")

    if not all_offers:
        print(f"[result] No source has {QUANTITY} seated together right now.")
        state.update(last_best_price=None, last_checked=now)
        save_state(state)
        return

    all_offers.sort(key=lambda o: o["price"])
    best = all_offers[0]
    price = best["price"]
    by_src = {}
    for o in all_offers:
        by_src.setdefault(o["source"], o["price"])
    summary = ", ".join(f"{s} ${p:,.0f}" for s, p in sorted(by_src.items(), key=lambda x: x[1]))
    print(f"[result] cheapest {QUANTITY}-together: ${price:,.0f}/tk on {best['source']}  ({summary})")

    # Alert decision.
    reasons = []
    if MAX_PRICE is not None:
        # Deal mode: only ping when at/below target (and don't re-spam same level).
        already = state.get("notified_at_or_below") is True
        if price <= MAX_PRICE and not (already and prev is not None and price >= prev):
            reasons.append(f"AT/BELOW your ${MAX_PRICE:,.0f} target")
        state["notified_at_or_below"] = price <= MAX_PRICE
    else:
        if prev is None:
            reasons.append(f"{QUANTITY} seats together are now AVAILABLE")
        elif price < prev:
            reasons.append(f"price DROPPED ${prev:,.0f} -> ${price:,.0f}/ticket")

    if reasons:
        total = price * QUANTITY
        subject = f"⚽ Match 95: {QUANTITY} together @ ${price:,.0f}/tk on {best['source']} — {reasons[0]}"
        body = (
            "World Cup Match 95 (Round of 16, Atlanta, Jul 7) ticket watch:\n\n"
            + "\n".join(f"• {r}" for r in reasons) + "\n\n"
            f"BEST: ${price:,.0f}/ticket all-in  (~${total:,.0f} for {QUANTITY})\n"
            f"  {best['source']} — Section {best['section']}, Row {best['row']} "
            f"(listing has {best['qty']})\n"
            f"  {best['url']}\n\n"
            f"Cheapest per market: {summary}\n"
            f"Checked {now}.")
        send_email(subject, body)
    else:
        print("[email] nothing worth alerting on; staying quiet.")

    state.update(
        last_best_price=price, last_checked=now,
        last_best={"source": best["source"], "section": best["section"],
                   "row": best["row"], "price": price, "total": price * QUANTITY},
        by_source=by_src)
    save_state(state)


if __name__ == "__main__":
    main()
