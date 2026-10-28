"""Scrape Jerusalem district committee meetings from Mavat and match plan numbers.

Primary path: download each meeting's agenda PDF (סדר יום) via a Playwright popup,
parse it with pdfplumber to extract plan + purpose (מטרת הדיון) + per-plan time.
Fallback: scrape the SV4 meeting detail page (no purpose available there).
"""

import asyncio
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta

from playwright.async_api import async_playwright

from agenda_pdf import download_agenda_pdf, parse_pdf_agenda

log = logging.getLogger(__name__)

MAVAT_BASE = "https://mavat.iplan.gov.il"
DAYS_BACK = 14
DAYS_FORWARD = 30
NO_PDF_PURPOSE = ""  # left blank in the email table when the agenda PDF was unavailable
# GitHub Actions caps check.yml at 20 min. Leave 3 min slack for mail+teardown.
# Per-meeting cap keeps a single stuck meeting from eating the whole run.
MEETING_LOOP_BUDGET_SEC = 15 * 60
PER_MEETING_BUDGET_SEC = 60


def _plans_dict_matches(plan_number: str, plans_dict: dict[str, str]) -> tuple[str, str] | None:
    """Return (eid, saved_plan_name) if plan_number '101-1382423' matches a key
    in plans_dict (which is keyed on bare entity ids like '1382423').
    """
    # Suffix of a '101-1382423' = '1382423', strip leading zeros to match parsers.py normalization.
    m = re.match(r"\d{3}-(\d{7})$", plan_number)
    if m:
        suffix = m.group(1).lstrip("0") or "0"
        if suffix in plans_dict:
            return suffix, plans_dict[suffix]
    # Also allow direct containment (same logic as the old scraper did).
    for eid, name in plans_dict.items():
        padded = eid.zfill(7)
        if plan_number.endswith(padded) or eid in plan_number:
            return eid, name
    return None


async def extract_plans_from_meeting_page(
    context,
    detail_href: str,
    plans_dict: dict[str, str],
    meeting_id: str,
    meeting_date: str,
) -> list[dict]:
    """Fallback used when the agenda PDF couldn't be opened. No purpose column."""
    matches = []
    detail_url = (
        f"{MAVAT_BASE}{detail_href}"
        if not detail_href.startswith("http")
        else detail_href
    )
    log.info("  נכנס לעמוד פרטי ישיבה: %s", detail_url)

    detail_page = await context.new_page()
    try:
        await detail_page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
        try:
            await detail_page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        await asyncio.sleep(3)

        detail_rows = await detail_page.query_selector_all("tr")
        log.info("  נטענו %d שורות בעמוד הישיבה", len(detail_rows))
        plan_info_map: dict[str, dict] = {}
        for dr in detail_rows:
            row_text = await dr.inner_text()
            plan_nums_in_row = re.findall(r"\d{3}-\d{7}", row_text)
            plan_nums_in_row = [
                p for p in plan_nums_in_row if not re.match(r"(05[0-9]|07[2-7])-", p)
            ]
            if not plan_nums_in_row:
                continue

            time_match = re.search(r"\b(\d{1,2}:\d{2})\b", row_text)
            row_time = time_match.group(1) if time_match else ""

            cleaned = row_text
            for pn in plan_nums_in_row:
                cleaned = cleaned.replace(pn, "")
            if time_match:
                cleaned = cleaned.replace(time_match.group(0), "")
            cleaned = re.sub(r"^\s*\d+\.?\s*", "", cleaned, flags=re.MULTILINE)
            candidate_lines = [l.strip() for l in cleaned.splitlines() if l.strip()]
            title = max(candidate_lines, key=len) if candidate_lines else ""

            for pn in plan_nums_in_row:
                if pn not in plan_info_map:
                    plan_info_map[pn] = {"time": row_time, "title": title}

        log.info("  נמצאו %d מספרי תכנית בסעיפי ישיבה", len(plan_info_map))

        for plan_num, info in plan_info_map.items():
            hit = _plans_dict_matches(plan_num, plans_dict)
            if not hit:
                continue
            eid, saved_name = hit
            final_name = saved_name or info.get("title", "")
            log.info(
                "    התאמה: entity %s → %s בשעה %s | %s",
                eid, plan_num, info.get("time") or "?", final_name or "(ללא שם)",
            )
            matches.append(
                {
                    "plan": plan_num,
                    "plan_name": final_name,
                    "purpose": NO_PDF_PURPOSE,
                    "meeting_title": f"ועדה מחוזית ירושלים {meeting_id}",
                    "meeting_date": meeting_date,
                    "meeting_time": info.get("time", ""),
                    "detail_url": detail_url,
                }
            )

    except Exception as e:
        log.warning("  שגיאה בעמוד פרטי ישיבה %s: %s", meeting_id, e)
    finally:
        await detail_page.close()

    return matches


async def extract_plans_from_agenda_pdf(
    context,
    row,
    plans_dict: dict[str, str],
    meeting_id: str,
    meeting_date: str,
    detail_url: str,
    tmp_dir: str,
) -> list[dict] | None:
    """Try to download+parse the agenda PDF. Returns matches list, or None if
    the PDF path was unavailable (caller should fall back to the detail page).
    An empty list means PDF parsed fine but none of our plans were on the agenda.
    """
    pdf_trigger = row.locator(
        'img[alt="ראה קובץ בפורמט PDF"], img[title*="PDF"], .clickable'
    ).first
    if await pdf_trigger.count() == 0:
        return None

    pdf_path = os.path.join(tmp_dir, f"{meeting_id}.pdf")
    ok = await download_agenda_pdf(context, pdf_trigger, pdf_path)
    if not ok:
        return None

    plan_info = parse_pdf_agenda(pdf_path)
    try:
        os.remove(pdf_path)
    except OSError:
        pass

    if not plan_info:
        # The PDF opened but didn't contain the expected columns (e.g. a scanned
        # agenda). Treat this as "no PDF-level purpose" and fall back so we at
        # least report the plan without a purpose.
        return None

    matches = []
    for plan_num, info in plan_info.items():
        hit = _plans_dict_matches(plan_num, plans_dict)
        if not hit:
            continue
        eid, saved_name = hit
        final_name = saved_name or info.get("name") or ""
        log.info(
            "    התאמה (PDF): entity %s → %s בשעה %s | מטרה: %s",
            eid, plan_num, info.get("time") or "?", info.get("purpose") or "(ריק)",
        )
        matches.append(
            {
                "plan": plan_num,
                "plan_name": final_name,
                "purpose": info.get("purpose", ""),
                "meeting_title": f"ועדה מחוזית ירושלים {meeting_id}",
                "meeting_date": meeting_date,
                "meeting_time": info.get("time", ""),
                "detail_url": detail_url,
            }
        )
    return matches


async def fetch_meetings_via_playwright(plans_dict: dict[str, str]) -> list[dict]:
    log.info("משתמש ב-Playwright לניווט ב-mavat")
    url = f"{MAVAT_BASE}/SV3?searchEntity=3&searchMethod=2"
    all_matches = []
    tmp_dir = tempfile.mkdtemp(prefix="mavat_agenda_")

    # Override the default Mavat search request so it returns meetings from
    # DAYS_BACK days ago up to DAYS_FORWARD days ahead, not just upcoming.
    from_date_iso = (
        datetime.utcnow() - timedelta(days=DAYS_BACK)
    ).replace(hour=21, minute=0, second=0, microsecond=0).isoformat(timespec="milliseconds") + "Z"
    to_date_iso = (
        datetime.utcnow() + timedelta(days=DAYS_FORWARD)
    ).replace(hour=21, minute=0, second=0, microsecond=0).isoformat(timespec="milliseconds") + "Z"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        async def handle_search(route):
            try:
                post = route.request.post_data
                if post:
                    import json as _json
                    body = _json.loads(post)
                    body["_fromDate"] = from_date_iso
                    body["_toDate"] = to_date_iso
                    body["toResult"] = 200
                    new_body = _json.dumps(body, ensure_ascii=False)
                    await route.continue_(post_data=new_body)
                    log.info(
                        "Mavat search: from=%s to=%s",
                        from_date_iso[:10], to_date_iso[:10],
                    )
                    return
            except Exception as e:
                log.warning("override search body נכשל: %s", e)
            await route.continue_()

        await page.route("**/rest/api/sv3/Search", handle_search)

        try:
            await page.goto(url, wait_until="networkidle", timeout=60000)
            log.info("עמוד mavat נטען. מגדיר פילטרים...")

            try:
                toggle = page.locator("text=חיפוש מתקדם").first
                if await toggle.is_visible():
                    await toggle.click()
                    await asyncio.sleep(2)
            except Exception:
                pass

            try:
                machuz_inputs = page.locator('input[name="מחוז"][role="searchbox"]')
                mcount = await machuz_inputs.count()
                machuz_input = None
                for idx in range(mcount):
                    el = machuz_inputs.nth(idx)
                    if await el.is_visible():
                        machuz_input = el
                        break
                if machuz_input:
                    await machuz_input.click()
                    await machuz_input.press_sequentially("ירושלים", delay=100)
                    await asyncio.sleep(1)
                    suggestion = page.locator(".p-autocomplete-items li").first
                    await suggestion.wait_for(state="visible", timeout=5000)
                    await suggestion.click()
                    log.info("נבחר מחוז: ירושלים")
                    await asyncio.sleep(1)
                else:
                    log.warning("לא נמצא שדה מחוז")
            except Exception as e:
                log.warning("שגיאה בבחירת מחוז: %s", e)

            try:
                search_btn = page.locator('button:has-text("חיפוש"):visible').first
                await search_btn.click()
                log.info("לחיצה על חיפוש. ממתין לתוצאות...")
                await asyncio.sleep(5)
            except Exception as e:
                log.warning("שגיאה בלחיצה על חיפוש: %s", e)

            try:
                await page.wait_for_selector(
                    ".loader, .loading", state="hidden", timeout=30000
                )
            except Exception:
                pass

            for i in range(15):
                show_more = page.locator('button:has-text("הצג עוד")').first
                if await show_more.is_visible():
                    log.info("טוען עוד תוצאות (%d)...", i + 1)
                    await show_more.scroll_into_view_if_needed()
                    await show_more.click()
                    await asyncio.sleep(5)
                else:
                    break

            rows = page.locator('tr:has-text("ירושלים")')
            count = await rows.count()
            log.info("נמצאו %d שורות ירושלים", count)

            cutoff = datetime.now() - timedelta(days=DAYS_BACK)
            loop_started = time.monotonic()
            skipped_over_budget = 0

            for i in range(count):
                if time.monotonic() - loop_started > MEETING_LOOP_BUDGET_SEC:
                    skipped_over_budget = count - i
                    log.warning(
                        "חרגנו מתקציב הישיבות (%ds). מדלגים על %d ישיבות נותרות.",
                        MEETING_LOOP_BUDGET_SEC, skipped_over_budget,
                    )
                    break

                row = rows.nth(i)
                row_text = await row.inner_text()
                meeting_id = "Unknown"
                meeting_date = "Unknown"
                id_match = re.search(r"202\d{4}", row_text)
                if id_match:
                    meeting_id = id_match.group(0)
                date_match = re.search(r"\d{2}/\d{2}/\d{4}", row_text)
                if date_match:
                    meeting_date = date_match.group(0)

                if meeting_date != "Unknown":
                    try:
                        m_date = datetime.strptime(meeting_date, "%d/%m/%Y")
                        if m_date < cutoff:
                            continue
                    except ValueError:
                        pass

                log.info("בודק ישיבה %s (%s)...", meeting_id, meeting_date)

                detail_link = row.locator('a[href*="/SV4/3/"]').first
                detail_href = ""
                if await detail_link.count() > 0:
                    detail_href = await detail_link.get_attribute("href") or ""
                detail_url = (
                    f"{MAVAT_BASE}{detail_href}"
                    if detail_href and not detail_href.startswith("http")
                    else detail_href
                )

                try:
                    # Try PDF first — it's the only path that yields מטרת הדיון.
                    pdf_matches = await asyncio.wait_for(
                        extract_plans_from_agenda_pdf(
                            context, row, plans_dict, meeting_id, meeting_date,
                            detail_url, tmp_dir,
                        ),
                        timeout=PER_MEETING_BUDGET_SEC,
                    )
                    if pdf_matches is not None:
                        all_matches.extend(pdf_matches)
                        log.info("  PDF: %d התאמות", len(pdf_matches))
                        continue

                    # PDF unavailable — fall back to the detail-page scrape (no purpose).
                    if detail_href:
                        log.info("  PDF לא זמין — נופל לעמוד פרטי ישיבה")
                        found = await asyncio.wait_for(
                            extract_plans_from_meeting_page(
                                context, detail_href, plans_dict,
                                meeting_id, meeting_date,
                            ),
                            timeout=PER_MEETING_BUDGET_SEC,
                        )
                        all_matches.extend(found)
                except asyncio.TimeoutError:
                    log.warning(
                        "ישיבה %s חרגה מ-%ds — מדלגים.",
                        meeting_id, PER_MEETING_BUDGET_SEC,
                    )
                except Exception as e:
                    log.warning("שגיאה בעיבוד ישיבה %s: %s", meeting_id, e)

        except Exception as e:
            log.error("שגיאה כללית ב-Playwright: %s", e)
        finally:
            await browser.close()
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass

    log.info("Playwright מצא %d התאמות", len(all_matches))
    return all_matches
