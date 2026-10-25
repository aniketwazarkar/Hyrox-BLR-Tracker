#!/usr/bin/env python3
"""Email me when HYROX Bengaluru 2027 men's tickets go on sale.

How it works:
  1. Fetch the hyrox.com event page. While closed it shows "Ticket sales start
     soon!"; once open it gets an "Athlete Tickets" button linking to the
     vivenu ticket shop (<region>.hyrox.com/event/...).
  2. Fetch that shop and read its embedded __NEXT_DATA__ JSON. Tickets are open
     when shop.saleStatus == "onSale" and a men's ticket type is active.
  3. Send one email, then write state.json so later runs stay quiet.

Env vars: SMTP_USER, SMTP_PASS, TO_EMAIL (email alerts).
         WHATSAPP_PHONE, WHATSAPP_APIKEY (WhatsApp alerts via textmebot.com).
Either channel is skipped if its vars are unset.
Optional: EVENT_URL (to test against another event), DRY_RUN=1, FORCE_TEST_EMAIL=1.
"""
import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

EVENT_URL = os.environ.get("EVENT_URL", "https://hyrox.com/event/hyrox-bengaluru/")
STATE_FILE = Path(__file__).with_name("state.json")
CLOSED_MARKER = "Ticket sales start soon"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


def fetch(url, attempts=3):
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="ignore")
        except Exception as e:
            if i == attempts - 1:
                raise
            print(f"fetch failed ({e}), retrying...")
            time.sleep(5 * (i + 1))


def find_ticket_links(page):
    """Return shop URLs from the page's ticket buttons."""
    links = []
    for href, label in re.findall(r'<a class="w-btn[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S):
        label = html.unescape(re.sub(r"<[^>]+>", "", label)).strip().lower()
        href = html.unescape(href)
        if "ticket" in label and "spectator" not in label and re.search(r"(hyrox\.com/event/|vivenu)", href):
            links.append(href)
    return list(dict.fromkeys(links))


def is_mens(name):
    n = name.upper()
    return re.search(r"\bMEN\b", n) and "WOMEN" not in n and "KLOOK" not in n


def mens_tickets_on_sale(shop_url):
    """Return (on_sale, [active men's ticket names]) for a vivenu shop URL."""
    page = fetch(shop_url)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        return None, []
    props = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
    shop = props.get("shop") or {}
    tickets = shop.get("tickets") or (props.get("event") or {}).get("tickets") or []
    mens = [t["name"] for t in tickets if t.get("active") and is_mens(t.get("name", ""))]
    return shop.get("saleStatus") == "onSale" and bool(mens), mens


def send_email(subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["TO_EMAIL"]
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)
    print(f"Email sent to {msg['To']}")


def send_whatsapp(text):
    query = urllib.parse.urlencode({
        "recipient": os.environ["WHATSAPP_PHONE"],
        "apikey": os.environ["WHATSAPP_APIKEY"],
        "text": text,
    })
    resp = fetch(f"https://api.textmebot.com/send.php?{query}")
    print(f"TextMeBot response: {re.sub(r'<[^>]+>', ' ', resp).strip()[:200]}")
    if re.search(r"error|invalid|not allowed", resp, re.I):
        raise RuntimeError("TextMeBot rejected the message")
    print("WhatsApp message sent")


def notify(subject, body, link):
    """Send on every configured channel. Raise only if none succeeded."""
    if os.environ.get("DRY_RUN"):
        print(f"[DRY_RUN] would notify:\n{subject}\n\n{body}")
        return
    channels = []
    if os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS") and os.environ.get("TO_EMAIL"):
        channels.append(("email", lambda: send_email(subject, body)))
    if os.environ.get("WHATSAPP_PHONE") and os.environ.get("WHATSAPP_APIKEY"):
        channels.append(("whatsapp", lambda: send_whatsapp(f"{subject}\n{link}")))
    if not channels:
        raise RuntimeError("No notification channel configured (set SMTP_* or WHATSAPP_* secrets)")
    errors = []
    for name, send in channels:
        try:
            send()
        except Exception as e:
            print(f"{name} failed: {e}", file=sys.stderr)
            errors.append(name)
    if len(errors) == len(channels):
        raise RuntimeError(f"All notification channels failed: {errors}")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def main():
    if os.environ.get("FORCE_TEST_EMAIL"):
        notify("HYROX watcher test", f"Your HYROX watcher can send alerts. Watching {EVENT_URL}", EVENT_URL)
        return

    state = load_state()
    page = fetch(EVENT_URL)
    links = find_ticket_links(page)
    closed = CLOSED_MARKER.lower() in page.lower()
    print(f"closed marker present: {closed}; ticket links: {links}")

    status, detail, buy_link = "closed", "", EVENT_URL
    for link in links:
        on_sale, mens = mens_tickets_on_sale(link)
        print(f"{link}: on_sale={on_sale} mens={mens}")
        if on_sale:
            status, buy_link = "open", link
            detail = f"Buy here: {link}\n\nMen's tickets on sale:\n" + "\n".join(f"  - {n}" for n in mens)
            break
    if status == "closed" and (links or not closed):
        # Page changed but we couldn't confirm men's tickets: worth a manual look.
        status = "changed"
        detail = "The page no longer looks like the 'coming soon' version, but I couldn't confirm men's tickets automatically.\n"
        detail += "\n".join(links) or "(no ticket button found)"

    print(f"status: {status}")
    if status == "closed" or state.get("notified") == status:
        return

    subject = {
        "open": "🚨 HYROX Bengaluru 2027 men's tickets are ON SALE",
        "changed": "HYROX Bengaluru page changed — check tickets",
    }[status]
    notify(subject, f"{detail}\n\nEvent page: {EVENT_URL}", buy_link)
    STATE_FILE.write_text(json.dumps({"notified": status, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
