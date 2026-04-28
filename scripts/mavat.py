"""Scrape Jerusalem district committee meetings from Mavat and match plan numbers."""

import asyncio
import logging
import re
from datetime import datetime, timedelta

from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

MAVAT_BASE = "https://mavat.iplan.gov.il"
DAYS_BACK = 14


async def extract_plans_from_meeting_page(
    context,
    detail_href: str,
    plans_dict: dict[str, str],
    meeting_id: str,
    meeting_date: str,
) -> list[dict]:
    matches = []
    detail_url = (
        f"{MAVAT_BASE}{detail_href}"
        if not detail_href.startswith("http")
        else detail_href
    )
    log.info("  נכנס לעמוד פרטי ישיבה: %s", detail_url)

    detail_page = await context.new_page()
    try:
        await detail_page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)

        detail_rows = await detail_page.query_selector_all("tr")
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
            candidate_lines = [
                l.strip() for l in cleaned.splitlines() if l.strip()
            ]
            title = max(candidate_lines, key=len) if candidate_lines else ""

            for pn in plan_nums_in_row:
                if pn not in plan_info_map:
                    plan_info_map[pn] = {"time": row_time, "title": title}

        log.info("  נמצאו %d מספרי תכנית בסעיפי ישיבה", len(plan_info_map))

        for eid, plan_name in plans_dict.items():
            padded = eid.zfill(7)
            for plan_num, info in plan_info_map.items():
                if plan_num.endswith(padded) or eid in plan_num:
                    final_name = plan_name or info.get("title", "")
                    log.info(
                        "    התאמה: entity %s → %s בשעה %s | %s",
                        eid,
                        plan_num,
                        info.get("time") or "?",
                        final_name or "(ללא שם)",
                    )
                    matches.append(
                        {
                            "plan": plan_num,
                            "plan_name": final_name,
                            "meeting_title": f"ועדה מחוזית ירושלים {meeting_id}",
                            "meeting_date": meeting_date,
                            "meeting_time": info.get("time", ""),
                            "detail_url": detail_url,
                        }
                    )
                    break

    except Exception as e:
        log.warning("  שגיאה בעמוד פרטי ישיבה %s: %s", meeting_id, e)
    finally:
        await detail_page.close()

    return matches


async def fetch_meetings_via_playwright(plans_dict: dict[str, str]) -> list[dict]:
    log.info("משתמש ב-Playwright לניווט ב-mavat")
    url = f"{MAVAT_BASE}/SV3?searchEntity=3&searchMethod=2"
    all_matches = []

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
                count = await machuz_inputs.count()
                machuz_input = None
                for idx in range(count):
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

            for i in range(count):
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

                if detail_href:
                    found = await extract_plans_from_meeting_page(
                        context, detail_href, plans_dict, meeting_id, meeting_date
                    )
                    all_matches.extend(found)

        except Exception as e:
            log.error("שגיאה כללית ב-Playwright: %s", e)
        finally:
            await browser.close()

    log.info("Playwright מצא %d התאמות", len(all_matches))
    return all_matches
