#!/usr/bin/env python3
"""
ice_relay.py
============
Runs on a schedule (GitHub Actions, every 3 hours). Each run:

    1. Checks the Gmail 'ice' label for an "ICE RELOAD" request.
  2. For each DMI source (WA / SE / NCE), lists the live DMI directory index
     and finds the latest chart timestamp.
  3. Compares against state.json (what we last sent). If newer -- or if a
     reload was requested -- fetches the PDF, rasterizes + shrinks it to
     TARGET_KB, zips it, and emails it to the boat address.
  4. Writes state.json back out (the workflow commits it to the repo).

DMI publishes an open Apache directory listing for each region folder, e.g.
    https://ocean.dmi.dk/arctic/images/MODIS/SouthEast_RIC/
listing files named YYYYMMDDHHMM.ISKO.pdf. Because that naming sorts
correctly as plain text, "latest file in the listing" == "latest chart" --
no guessing at posting times needed.
"""

import email
import imaplib
import io
import os
import re
import smtplib
import sys
import zipfile
from email.message import EmailMessage
from pathlib import Path

import requests
import fitz  # PyMuPDF
from PIL import Image

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------

GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]

BOAT_ADDRESS = os.environ.get("BOAT_ADDRESS", GMAIL_ADDRESS)  # who replies go to
ICE_LABEL = "ice"  # Gmail IMAP folder name for the reload-request label

IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

TARGET_KB = 100  # hard ceiling per attachment
STATE_FILE = Path(__file__).parent / "state.json"

DMI_INDEX = "https://ocean.dmi.dk/arctic/images/MODIS/{folder}/"
DMI_PDF = "https://ocean.dmi.dk/arctic/images/MODIS/{folder}/{ts}.ISKO.pdf"

SOURCES = {
    "WA": {
        "folder": "Greenland_WA",
        "label": "DMI Whole Greenland overview",
    },
    "SE": {
        "folder": "SouthEast_RIC",
        "label": "DMI South East Greenland (Cape Farewell -> Skjoldungen / Tasiilaq)",
    },
    "NCE": {
        "folder": "NorthAndCentralEast_RIC",
        "label": "DMI North & Central East Greenland (Skjoldungen -> Scoresbysund)",
    },
}

TS_RE = re.compile(r'(\d{12})\.ISKO\.pdf')
REQUEST_RE = re.compile(r'\bICE\s+RELOAD\b', re.IGNORECASE)


# --------------------------------------------------------------------------
# STATE
# --------------------------------------------------------------------------

def load_state():
    import json
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    import json
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


# --------------------------------------------------------------------------
# DISCOVERY -- list the DMI directory, find the latest timestamp
# --------------------------------------------------------------------------

def find_latest(folder):
    url = DMI_INDEX.format(folder=folder)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    timestamps = TS_RE.findall(resp.text)
    if not timestamps:
        raise RuntimeError(f"No charts found in listing at {url}")
    return sorted(timestamps)[-1]


# --------------------------------------------------------------------------
# FETCH + COMPRESS
# --------------------------------------------------------------------------

def fetch_pdf(folder, ts):
    url = DMI_PDF.format(folder=folder, ts=ts)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content


def pdf_to_image(pdf_bytes, dpi=150):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    zoom = dpi / 72
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY)
    return Image.open(io.BytesIO(pix.tobytes("png")))


def compress_to_target(img, target_kb=TARGET_KB, max_width=900):
    img = img.convert("L")
    width = min(img.width, max_width)
    buf = None

    for _ in range(12):
        h = int(img.height * (width / img.width))
        resized = img.resize((width, h), Image.LANCZOS)
        for quality in (70, 55, 40, 30, 20, 12):
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() / 1024 <= target_kb:
                return buf.getvalue(), width, quality, buf.tell() / 1024
        width = int(width * 0.85)
        if width < 250:
            break

    return buf.getvalue(), width, quality, buf.tell() / 1024


def zip_bytes(data_bytes, filename):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr(filename, data_bytes)
    return buf.getvalue()


def build_attachment(source_code, ts):
    cfg = SOURCES[source_code]
    pdf_bytes = fetch_pdf(cfg["folder"], ts)
    img = pdf_to_image(pdf_bytes)
    jpeg_bytes, width, quality, size_kb = compress_to_target(img)
    fname = f"ICE_{source_code}_{ts}.jpg"
    print(f"[{source_code}] {ts} -> {width}px @ q{quality} -> {size_kb:.1f} KB")
    return zip_bytes(jpeg_bytes, fname), fname.replace(".jpg", ".zip")


# --------------------------------------------------------------------------
# EMAIL IN / OUT
# --------------------------------------------------------------------------

def check_reload_request():
    """Return True if an unread 'ICE RELOAD' email is sitting in the ice label."""
    with imaplib.IMAP4_SSL(IMAP_HOST) as imap:
        imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        status, data = imap.select(ICE_LABEL)
        if status != "OK":
            detail = data[0].decode(errors="replace") if data and data[0] else "unknown IMAP error"
            print(
                f"Reload check skipped: could not open Gmail label '{ICE_LABEL}': {detail}",
                file=sys.stderr,
            )
            return False
        status, data = imap.search(None, "UNSEEN")
        uids = data[0].split()
        found = False
        for num in uids:
            status, msg_data = imap.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            subject = msg.get("Subject", "")
            if REQUEST_RE.search(subject):
                found = True
                imap.store(num, "+FLAGS", "\\Seen")  # mark handled
        return found


def send_chart(source_code, ts, attachment_bytes, filename):
    cfg = SOURCES[source_code]
    msg = EmailMessage()
    msg["Subject"] = f"ICE {source_code} {ts}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = BOAT_ADDRESS
    msg.set_content(
        f"{cfg['label']}\n"
        f"Chart timestamp: {ts}\n"
        f"File: {filename} ({len(attachment_bytes)/1024:.1f} KB)\n"
    )
    msg.add_attachment(
        attachment_bytes, maintype="application", subtype="zip", filename=filename
    )
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------

def run():
    state = load_state()
    reload_requested = check_reload_request()
    if reload_requested:
        print("ICE RELOAD request found -- forcing a full check.")

    sent_any = False
    for code, cfg in SOURCES.items():
        try:
            latest_ts = find_latest(cfg["folder"])
        except Exception as e:
            print(f"FAILED discovery for {code}: {e}", file=sys.stderr)
            continue

        last_sent = state.get(code)
        if latest_ts == last_sent and not reload_requested:
            print(f"[{code}] no change ({latest_ts})")
            continue

        try:
            attachment, filename = build_attachment(code, latest_ts)
            send_chart(code, latest_ts, attachment, filename)
            state[code] = latest_ts
            sent_any = True
            print(f"[{code}] sent {filename}")
        except Exception as e:
            print(f"FAILED build/send for {code} @ {latest_ts}: {e}", file=sys.stderr)

    save_state(state)
    if not sent_any:
        print("Nothing new to send this run.")


if __name__ == "__main__":
    run()
