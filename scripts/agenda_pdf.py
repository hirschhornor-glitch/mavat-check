"""Download and parse Mavat agenda PDFs (סדר יום) to extract 'מטרת הדיון' per plan.

The purpose column exists only in the agenda PDF, not in Mavat's SV4 web view
(ported from Oranim's process_mavat_meetings.py).

Live inspection of SV3 showed the PDF click triggers a REST call to
/rest/api/Attacments/?eid=…&fn=<meeting_id>_סדר יום.pdf, then wraps the
response bytes in a Blob and calls window.open(blob_url). In headless
Chromium the popup opens unreliably (user-activation quirks), so we skip
the popup entirely: register a context-wide response listener BEFORE the
click, grab the response body directly when it arrives, and never need the
blob URL.
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


_AGENDA_URL_RE = re.compile(
    r"/rest/api/Attacments/.*?(?:%D7%A1%D7%93%D7%A8|סדר).*?%D7%99%D7%95%D7%9D|"
    r"/rest/api/Attacments/.*?סדר יום",
    re.IGNORECASE,
)


def _is_agenda_response(url: str, meeting_id: str) -> bool:
    """Match the Attacments response for THIS meeting's agenda PDF."""
    if "/rest/api/Attacments" not in url:
        return False
    if meeting_id and meeting_id not in url:
        return False
    # Encoded or raw Hebrew for "סדר יום"
    return (
        "%D7%A1%D7%93%D7%A8" in url  # ס ד ר
        or "סדר" in url
        or f"{meeting_id}_" in url  # the agenda is always this meeting's _סדר יום.pdf
    )


async def download_agenda_pdf(
    context: Any,
    pdf_trigger: Any,
    out_path: str,
    meeting_id: str = "",
    timeout_ms: int = 15_000,
) -> bool:
    """Click the agenda PDF icon, intercept the REST response, save body to out_path.

    Does NOT rely on a popup opening — in headless Chromium the popup fires
    unreliably due to user-activation quirks. Instead we register a context
    response listener before the click, grab the first response matching this
    meeting's /rest/api/Attacments URL, and read its body directly.

    Returns True if a PDF was saved, False otherwise. Never raises.
    """
    loop = asyncio.get_running_loop()
    pdf_future: asyncio.Future = loop.create_future()

    def on_response(resp):
        if pdf_future.done():
            return
        try:
            if _is_agenda_response(resp.url, meeting_id):
                pdf_future.set_result(resp)
        except Exception:
            pass

    context.on("response", on_response)
    try:
        try:
            await pdf_trigger.click()
        except Exception as e:
            log.warning("agenda PDF click failed for %s: %s", meeting_id, e)
            return False
        try:
            resp = await asyncio.wait_for(pdf_future, timeout=timeout_ms / 1000)
        except asyncio.TimeoutError:
            log.warning("no agenda response within %dms for %s", timeout_ms, meeting_id)
            return False
        try:
            body = await resp.body()
        except Exception as e:
            log.warning("agenda response body read failed for %s: %s", meeting_id, e)
            return False
        if not body or len(body) < 500:
            log.warning("agenda response too small (%d bytes) for %s", len(body or b""), meeting_id)
            return False
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(body)
        return True
    finally:
        try:
            context.remove_listener("response", on_response)
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
