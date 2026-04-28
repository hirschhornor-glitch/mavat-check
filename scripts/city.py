"""Scrape Jerusalem municipality local TABA committee meetings (ועדה מקומית — תב"ע).

Adapted from C:/ORANIM/process_jerusalem_taba.py to take a plans_dict input
and run headless for GitHub Actions.
"""

import asyncio
import logging
import re
import urllib.parse
from datetime import datetime, timedelta

import fitz
from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

COMMITTEE_URL = (
    "https://www.jerusalem.muni.il/he/city/council/committees/Committee"
    "?id=dba7382c-30e4-ee11-904c-000d3ab1f351"
)
DAYS_BACK = 14
DAYS_FORWARD = 30
PDF_KEYWORDS = ("ריכוז", "סדר יום", "תכניות")
DISCUSSION_KEYWORDS = ("התנגדויות", "הפקדה", "דיון", "אישור", "תיקון")


def _suffix_map(plans_dict: dict[str, str]) -> dict[str, str]:
    """{entity_id_stripped: entity_id_original} for fast suffix lookup."""
    return {eid.lstrip("0"): eid for eid in plans_dict.keys()}


def _parse_pdf_for_matches(
    pdf_bytes: bytes,
    plans_dict: dict[str, str],
    meeting_title: str,
    meeting_date: str,
    meeting_time: str,
    meeting_url: str,
) -> list[dict]:
    matches: list[dict] = []
    suffix_to_full = _suffix_map(plans_dict)
    seen_in_pdf: set[str] = set()

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        log.warning("שגיאה בפתיחת PDF: %s", e)
        return matches

    try:
        max_pages = min(len(doc), 10)
        for page_num in range(max_pages):
            text = doc[page_num].get_text()

            if page_num >= 4 and re.search(r"נושא\s+מס", text):
                log.info("  עוצר בעמוד %d (התחלת סעיפים מפורטים)", page_num + 1)
                break

            lines = [l.strip() for l in text.split("\n")]

            for i, line in enumerate(lines):
                plan_match = re.search(
                    r"(\d{3}-\d{5,})|(?<!\d)(\d{6,8})(?!\d)", line
                )
                if not plan_match:
                    continue

                raw_id = plan_match.group(0).replace("101-", "").lstrip("0")
                eid = suffix_to_full.get(raw_id)
                if not eid or eid in seen_in_pdf:
                    continue

                lookahead = " ".join(lines[i : i + 30])
                discussion = next(
                    (w for w in DISCUSSION_KEYWORDS if w in lookahead), ""
                )
                time_match = re.search(r"\b(\d{1,2}:\d{2})\b", lookahead)
                row_time = time_match.group(1) if time_match else meeting_time

                fallback_title = re.sub(
                    r"(\d{3}-\d{5,})|(?<!\d)(\d{6,8})(?!\d)", "", line
                )
                fallback_title = re.sub(r"^\s*\d+[\.\)]?\s*", "", fallback_title)
                fallback_title = fallback_title.strip(" \t-:.,")
                final_name = plans_dict.get(eid, "") or fallback_title

                seen_in_pdf.add(eid)
                matches.append(
                    {
                        "plan": eid,
                        "plan_name": final_name,
                        "meeting_title": meeting_title,
                        "meeting_date": meeting_date,
                        "meeting_time": row_time,
                        "detail_url": meeting_url,
                        "source": "city",
                        "discussion_type": discussion,
                    }
                )
                log.info(
                    "  התאמה (עירייה): entity %s → %s | %s | %s",
                    eid,
                    line.strip()[:60],
                    discussion or "?",
                    row_time or "?",
                )
    finally:
        doc.close()

    return matches


async def fetch_city_meetings(plans_dict: dict[str, str]) -> list[dict]:
    """Visit Jerusalem TABA committee page, find recent/upcoming meetings,
    download agenda PDFs, and return matches against plans_dict."""
    log.info("בודק ועדה מקומית ירושלים (תב\"ע) ב-%s", COMMITTEE_URL)
    all_matches: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            await page.goto(COMMITTEE_URL, wait_until="networkidle", timeout=60000)
            try:
                await page.wait_for_selector(".meeting-item, tr", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(5)

            today = datetime.now().date()
            window_back = today - timedelta(days=DAYS_BACK)
            window_forward = today + timedelta(days=DAYS_FORWARD)

            meeting_rows = await page.locator(".meeting-item, tr, .list-item").all()
            meetings: list[dict] = []
            for row in meeting_rows:
                text = await row.inner_text()
                date_match = re.search(r"(\d{2})[\./](\d{2})[\./](\d{4})", text)
                if not date_match:
                    continue
                d, m, y = date_match.groups()
                try:
                    mdate = datetime(int(y), int(m), int(d)).date()
                except ValueError:
                    continue
                if not (window_back <= mdate <= window_forward):
                    continue

                time_match = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", text)
                mtime = time_match.group(0) if time_match else ""
                link_loc = row.locator("a").first
                if await link_loc.count() == 0:
                    continue
                href = await link_loc.get_attribute("href")
                if not href or href == "#":
                    continue

                meetings.append(
                    {
                        "date": mdate,
                        "time": mtime,
                        "href": href,
                        "title": text.strip().split("\n")[0][:120],
                    }
                )

            if not meetings:
                log.info("לא נמצאו ישיבות בחלון הזמן")
                return all_matches

            seen_hrefs = set()
            unique_meetings = []
            for mt in sorted(meetings, key=lambda x: x["date"]):
                if mt["href"] in seen_hrefs:
                    continue
                seen_hrefs.add(mt["href"])
                unique_meetings.append(mt)

            log.info("נמצאו %d ישיבות לבדיקה", len(unique_meetings))

            for target in unique_meetings:
                full_url = urllib.parse.urljoin(page.url, target["href"])
                date_str = target["date"].strftime("%d/%m/%Y")
                log.info("\n--- ישיבה: %s ---", date_str)

                try:
                    await page.goto(full_url, wait_until="networkidle", timeout=60000)
                    await asyncio.sleep(5)
                except Exception as e:
                    log.warning("שגיאה בטעינת %s: %s", full_url, e)
                    continue

                pdf_links = await page.locator('a[href*=".pdf"]').all()
                pdf_urls: list[dict] = []
                for link in pdf_links:
                    href = await link.get_attribute("href")
                    if not href:
                        continue
                    if not href.startswith("http"):
                        href = urllib.parse.urljoin(page.url, href)
                    desc = (await link.inner_text() or "").strip()
                    pdf_urls.append({"url": href, "desc": desc})

                relevant = [
                    p
                    for p in pdf_urls
                    if any(k in p["desc"] for k in PDF_KEYWORDS)
                ] or pdf_urls[:2]

                meeting_title = f"ועדה מקומית ירושלים — תב\"ע ({date_str})"

                for pdf_info in relevant[:3]:
                    try:
                        resp = await page.request.get(pdf_info["url"])
                        if not resp.ok:
                            continue
                        pdf_bytes = await resp.body()
                    except Exception as e:
                        log.warning("שגיאה בהורדת PDF: %s", e)
                        continue

                    pdf_matches = _parse_pdf_for_matches(
                        pdf_bytes,
                        plans_dict,
                        meeting_title,
                        date_str,
                        target["time"],
                        full_url,
                    )
                    all_matches.extend(pdf_matches)

        except Exception as e:
            log.error("שגיאה כללית בבדיקת ועדה מקומית: %s", e)
        finally:
            await browser.close()

    log.info("ועדה מקומית: %d התאמות", len(all_matches))
    return all_matches
