"""Download and parse Mavat agenda PDFs (סדר יום) to extract 'מטרת הדיון' per plan.

The purpose column exists only in the agenda PDF, not in Mavat's SV4 web view
(ported from Oranim's process_mavat_meetings.py). Clicking the PDF icon opens
a Chromium popup with a blob: URL; we read the blob through the popup and save it.
"""

import asyncio
import base64
import logging
import os
import re
from typing import Any

import pdfplumber

log = logging.getLogger(__name__)

# Hebrew substrings that only appear when PDF text came out character-reversed
# (visual order). If any show up, the whole string gets reversed back.
_REVERSED_MARKERS = (
    "תינכות", "ןוידה", "אשונ", "בושי", "תויודגנתהב",
    "ןויד", "ןוקית", "תועט", "יוניב", "תינכתב", "חותיפו",
)

# Words that a correctly-ordered מטרת-הדיון cell starts with. If the first word
# isn't one of these, the words are assumed to be in reversed order.
_ACTION_STARTERS = (
    "דיון", "אישור", "הפקדה", "התנגדויות", "בקשה",
    "שינוי", "מתן", "הארכת", "ביטול",
)


def clean_hebrew(text: str) -> str:
    """Fix character-order flips and strip extraction artifacts."""
    if not text:
        return ""
    if any(m in text for m in _REVERSED_MARKERS):
        text = text[::-1]
    text = re.sub(r"^[\s\.\-\:]+", "", text)
    text = text.replace(" . ", ". ")
    text = re.sub(r"\.(\S)", r". \1", text)  # ".בהפקדת" → ". בהפקדת"
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\.\s*$", "", text).strip()
    return text


def fix_purpose_word_order(text: str) -> str:
    """If the first word isn't an action word, reverse word order."""
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    words = text.split()
    if not words:
        return ""
    first_clean = re.sub(r"[\.\-\:]+", "", words[0])
    if first_clean not in _ACTION_STARTERS:
        words = words[::-1]
    result = " ".join(words)
    result = re.sub(r"\.\s+", ". ", result)
    result = re.sub(r"^\.\s*", "", result)
    result = re.sub(r"\.\s*$", "", result)
    return result.strip()


async def download_agenda_pdf(
    context: Any, pdf_trigger: Any, out_path: str, timeout_ms: int = 8_000
) -> bool:
    """Click the agenda PDF icon, catch the resulting popup, save blob to out_path.

    Returns True if a PDF was saved, False otherwise. Never raises — the caller
    falls back to the SV4 detail-page scrape when this returns False.
    Timeouts are kept tight because this runs inside a 20-min GitHub Actions
    budget and most Jerusalem committee rows do open a popup within a few seconds.
    """
    popup = None
    try:
        async with context.expect_page(timeout=timeout_ms) as popup_info:
            await pdf_trigger.click()
        popup = await popup_info.value
        try:
            await popup.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:
            pass
        await asyncio.sleep(2)  # let the blob URL become readable

        blob_url = popup.url
        if not blob_url.startswith("blob:"):
            log.warning("agenda popup URL isn't a blob: %s", blob_url[:80])
            return False

        b64 = await popup.evaluate(
            """async (url) => {
                const res = await fetch(url);
                const b = await res.blob();
                return new Promise(r => {
                    const reader = new FileReader();
                    reader.onloadend = () => r(reader.result.split(',')[1]);
                    reader.readAsDataURL(b);
                });
            }""",
            blob_url,
        )
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(base64.b64decode(b64))
        return True
    except Exception as e:
        log.warning("agenda PDF download failed: %s", e)
        return False
    finally:
        try:
            if popup is not None:
                await popup.close()
        except Exception:
            pass


def parse_pdf_agenda(pdf_path: str) -> dict[str, dict]:
    """Return {plan_number '101-1382423': {purpose, time, name}} from the agenda PDF.

    Returns an empty dict if the PDF couldn't be parsed or doesn't contain a
    purpose column — caller then falls back to the no-purpose path.
    """
    out: dict[str, dict] = {}
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables(
                    {"vertical_strategy": "lines", "horizontal_strategy": "lines"}
                )
                if not tables:
                    tables = page.extract_tables(
                        {"vertical_strategy": "text", "horizontal_strategy": "text"}
                    )
                for table in tables or []:
                    if not table or len(table) < 2:
                        continue
                    headers = [
                        str(c).replace("\n", " ") if c else "" for c in table[0]
                    ]
                    col_subject = col_purpose = col_time = -1
                    for idx, h in enumerate(headers):
                        if "נושא" in h or "אשונ" in h:
                            col_subject = idx
                        if "מטרת הדיון" in h or "ןוידה תרטמ" in h or "מטרה" in h:
                            col_purpose = idx
                        if "שעה" in h or "העש" in h:
                            col_time = idx

                    if col_subject == -1 or col_purpose == -1:
                        continue

                    for row in table[1:]:
                        if len(row) <= col_subject:
                            continue
                        subj_cell = (
                            str(row[col_subject]).replace("\n", " ")
                            if row[col_subject]
                            else ""
                        )
                        id_match = re.search(r"\d{3}-\d{7}", subj_cell)
                        if not id_match:
                            continue
                        plan_number = id_match.group(0)
                        if plan_number in out:
                            continue  # first row wins
                        purp_cell = (
                            str(row[col_purpose]).replace("\n", " ")
                            if col_purpose != -1 and row[col_purpose]
                            else ""
                        )
                        time_cell = ""
                        if col_time != -1 and len(row) > col_time and row[col_time]:
                            tm = re.search(
                                r"\b(\d{1,2}:\d{2})\b", str(row[col_time])
                            )
                            if tm:
                                time_cell = tm.group(1)
                        name = clean_hebrew(subj_cell.replace(plan_number, ""))
                        purpose = fix_purpose_word_order(clean_hebrew(purp_cell))
                        out[plan_number] = {
                            "purpose": purpose,
                            "time": time_cell,
                            "name": name,
                        }
    except Exception as e:
        log.warning("pdfplumber failed on %s: %s", pdf_path, e)
    return out
