#!/usr/bin/env python3
"""Send the latest issue to subscribers through Resend.

Needs RESEND_API_KEY, EXPORT_KEY and FROM_EMAIL in the environment. The weekly
workflow skips this step until those secrets exist.
"""
import os
import sys
from pathlib import Path

import requests

SITE_URL = "https://causeway-weekly.vercel.app"
RESEND_BATCH = "https://api.resend.com/emails/batch"
BATCH_SIZE = 100
TIMEOUT = 30


def main():
    api_key = os.environ["RESEND_API_KEY"]
    export_key = os.environ["EXPORT_KEY"]
    sender = os.environ["FROM_EMAIL"]
    title = Path("public/latest-title.txt").read_text().strip()
    date = title.split(" ")[2].rstrip(":")
    body = Path(f"public/issues/{date}.html").read_text()
    r = requests.get(f"{SITE_URL}/api/subscribers", params={"key": export_key}, timeout=TIMEOUT)
    r.raise_for_status()
    emails = r.json()["emails"]
    if not emails:
        print("no subscribers yet")
        return
    for i in range(0, len(emails), BATCH_SIZE):
        batch = [{"from": sender, "to": [e], "subject": title, "html": body} for e in emails[i:i + BATCH_SIZE]]
        resp = requests.post(RESEND_BATCH, headers={"Authorization": f"Bearer {api_key}"}, json=batch, timeout=TIMEOUT)
        if resp.status_code >= 300:
            print("resend error", resp.status_code, resp.text, file=sys.stderr)
            sys.exit(1)
    print(f"sent to {len(emails)} subscribers")


if __name__ == "__main__":
    main()
