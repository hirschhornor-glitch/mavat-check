#!/usr/bin/env python3
"""MavatCheck — entry point.

Receives a file (base64) or URL + recipient email, extracts plan numbers,
checks Mavat Jerusalem committee agendas for the next 14 days, emails results.
"""

import argparse
import asyncio
import logging
import re
import sys
import traceback

from city import fetch_city_meetings
from mailer import send_error_email, send_results_email
from mavat import fetch_meetings_via_playwright
from parsers import parse_plans_from_file, parse_plans_from_url

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
log = logging.getLogger("mavatcheck")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--email", required=True)
    p.add_argument("--file-b64", default="")
    p.add_argument("--file-name", default="")
    p.add_argument("--url", default="")
    return p.parse_args()


async def run(args: argparse.Namespace) -> int:
    if not EMAIL_RE.match(args.email):
        log.error("מייל לא תקין: %s", args.email)
        return 2

    if not args.file_b64 and not args.url:
        send_error_email(args.email, "לא סופק קובץ ולא קישור — אין מקור לבדיקה.")
        return 2

    try:
        if args.file_b64:
            log.info("מקור: קובץ %s", args.file_name or "(ללא שם)")
            plans_dict = parse_plans_from_file(args.file_b64, args.file_name)
        else:
            log.info("מקור: URL %s", args.url)
            plans_dict = await parse_plans_from_url(args.url)
    except Exception as e:
        log.exception("שגיאה בחילוץ תכניות מהמקור")
        send_error_email(args.email, f"שגיאה בקריאת המקור: {e}")
        return 1

    if not plans_dict:
        send_error_email(args.email, "לא זוהו מספרי תכנית במקור שסופק.")
        return 1

    matches: list[dict] = []
    errors: list[str] = []

    try:
        mavat_matches = await fetch_meetings_via_playwright(plans_dict)
        for m in mavat_matches:
            m.setdefault("source", "mavat")
        matches.extend(mavat_matches)
    except Exception as e:
        log.exception("שגיאה בגישה ל-Mavat")
        errors.append(f"Mavat: {e}")

    try:
        city_matches = await fetch_city_meetings(plans_dict)
        matches.extend(city_matches)
    except Exception as e:
        log.exception("שגיאה בגישה לאתר העירייה")
        errors.append(f"עירייה: {e}")

    if errors and not matches:
        send_error_email(
            args.email,
            "שגיאה בגישה למקורות הבדיקה:\n" + "\n".join(errors),
        )
        return 1

    seen_keys = set()
    deduped = []
    for m in matches:
        key = f"{m.get('source','')}::{m['plan']}::{m['meeting_date']}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(m)

    log.info(
        "נמצאו %d התאמות (%d לפני סינון כפילויות) על %d תכניות",
        len(deduped),
        len(matches),
        len(plans_dict),
    )

    try:
        send_results_email(
            args.email,
            deduped,
            plans_count=len(plans_dict),
            partial_errors=errors,
        )
    except Exception:
        log.exception("שליחת מייל נכשלה")
        return 1

    return 0


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run(args))
    except Exception as e:
        log.error("שגיאה לא צפויה: %s\n%s", e, traceback.format_exc())
        try:
            send_error_email(args.email, f"שגיאה לא צפויה: {e}")
        except Exception:
            log.exception("גם מייל השגיאה נכשל")
        return 1


if __name__ == "__main__":
    sys.exit(main())
