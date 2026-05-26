#!/usr/bin/env python3
"""Read subscriptions.json and run MavatCheck for each subscription due today.

Frequencies:
  - "daily": runs every day
  - "weekly": runs only on Sundays (UTC)

Each subscription is independent; failures are isolated and logged.
"""

import argparse
import asyncio
import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from city import fetch_city_meetings
from mailer import send_error_email, send_results_email
from mavat import fetch_meetings_via_playwright
from parsers import parse_plans_from_url

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
log = logging.getLogger("subscriptions")

SUBSCRIPTIONS_PATH = Path(__file__).parent.parent / "subscriptions.json"


def is_due(frequency: str, today: datetime) -> bool:
    if frequency == "daily":
        return True
    if frequency == "weekly":
        # Sunday in Python's weekday() is 6 (Mon=0, Sun=6)
        return today.weekday() == 6
    return False


async def run_one(sub: dict, welcome_message: str | None = None) -> tuple[bool, str]:
    email = sub.get("email", "").strip()
    url = sub.get("url", "").strip()
    if not email or not url:
        return False, "missing email or url"

    try:
        plans_dict = await parse_plans_from_url(url)
    except Exception as e:
        send_error_email(email, f"שגיאה בקריאת המקור {url}: {e}")
        return False, f"parse_plans: {e}"

    if not plans_dict:
        send_error_email(email, f"לא זוהו מספרי תכנית במקור: {url}")
        return False, "no plans"

    matches: list[dict] = []
    errors: list[str] = []

    try:
        mavat_matches = await fetch_meetings_via_playwright(plans_dict)
        for m in mavat_matches:
            m.setdefault("source", "mavat")
        matches.extend(mavat_matches)
    except Exception as e:
        log.exception("Mavat error for %s", email)
        errors.append(f"Mavat: {e}")

    try:
        city_matches = await fetch_city_meetings(plans_dict)
        matches.extend(city_matches)
    except Exception as e:
        log.exception("City error for %s", email)
        errors.append(f"עירייה: {e}")

    seen = set()
    deduped = []
    for m in matches:
        key = f"{m.get('source','')}::{m['plan']}::{m['meeting_date']}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(m)

    try:
        send_results_email(
            email,
            deduped,
            plans_count=len(plans_dict),
            partial_errors=errors,
            welcome_message=welcome_message,
        )
    except Exception as e:
        return False, f"send_email: {e}"

    return True, f"{len(deduped)} matches"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--force-email",
        default="",
        help="Run only this email, ignoring the frequency schedule.",
    )
    p.add_argument(
        "--welcome",
        action="store_true",
        help="Include a welcome banner in the email (use with --force-email).",
    )
    return p.parse_args()


WELCOME_TEXT = (
    "<strong>ברוכים הבאים ל-MavatCheck.</strong> "
    "ההרשמה הושלמה לבדיקה שבועית של הקישור שנשלח. "
    "מעכשיו יישלח מייל אוטומטי <strong>בכל יום ראשון בבוקר (09:00)</strong> "
    "עם תוצאות הבדיקה — גם בשבועות שבהם לא נמצאו תכניות התואמות."
)


async def main() -> int:
    args = parse_args()

    if not SUBSCRIPTIONS_PATH.exists():
        log.info("subscriptions.json לא קיים — אין מנויים")
        return 0

    try:
        subs = json.loads(SUBSCRIPTIONS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.error("שגיאה בקריאת subscriptions.json: %s", e)
        return 1

    today = datetime.now(timezone.utc)
    log.info(
        "התחלת ריצה — %d מנויים, יום %s, force_email=%r, welcome=%s",
        len(subs),
        today.strftime("%A %Y-%m-%d"),
        args.force_email,
        args.welcome,
    )

    force_email = args.force_email.strip().lower()
    welcome_message = WELCOME_TEXT if args.welcome else None

    results = []
    for sub in subs:
        sub_email = sub.get("email", "").strip().lower()

        if force_email:
            if sub_email != force_email:
                continue
        else:
            freq = sub.get("frequency", "weekly")
            if not is_due(freq, today):
                log.info("מדלג: %s (%s — לא היום)", sub.get("email", "?"), freq)
                continue

        log.info("מריץ: %s | %s", sub.get("email", "?"), sub.get("url", "?"))
        try:
            ok, msg = await run_one(sub, welcome_message=welcome_message)
            results.append((sub.get("email", "?"), ok, msg))
            log.info("  → %s | %s", "OK" if ok else "FAIL", msg)
        except Exception as e:
            log.error(
                "שגיאה לא צפויה ב-%s: %s\n%s",
                sub.get("email", "?"),
                e,
                traceback.format_exc(),
            )
            results.append((sub.get("email", "?"), False, f"unexpected: {e}"))

    if force_email and not results:
        log.error("force_email=%s לא נמצא ב-subscriptions.json", force_email)
        return 1

    succeeded = sum(1 for _, ok, _ in results if ok)
    log.info("סיכום: %d/%d הצליחו", succeeded, len(results))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
