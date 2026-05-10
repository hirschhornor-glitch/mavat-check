"""Parse plan numbers from uploaded files (xlsx/csv) or arbitrary URLs."""

import asyncio
import base64
import io
import logging
import re
import urllib.request

import pandas as pd
from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

PLAN_PATTERN_FULL = re.compile(r"\d{3}-\d{7}")
PLAN_PATTERN_ENTITY = re.compile(r"^\d{5,7}$")
PHONE_PREFIX = re.compile(r"^(05[0-9]|07[2-7])-")
GOOGLE_SHEETS_RE = re.compile(
    r"https?://docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)"
)

HEADER_HINTS = ("תכנית", "מספר תכנית", "plan", "agam", "entity")


def _normalize_to_entity_id(value: str) -> str | None:
    """Return entity id (5-7 digits) from a value like '101-0123456' or '123456'."""
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return None

    full = PLAN_PATTERN_FULL.search(s)
    if full:
        if PHONE_PREFIX.match(full.group(0)):
            return None
        suffix = full.group(0).split("-", 1)[1]
        return suffix.lstrip("0") or "0"

    if PLAN_PATTERN_ENTITY.match(s):
        return s.lstrip("0") or "0"

    digits_only = re.sub(r"\D", "", s)
    if PLAN_PATTERN_ENTITY.match(digits_only):
        return digits_only.lstrip("0") or "0"

    return None


def _read_dataframe(content: bytes, filename: str) -> pd.DataFrame:
    name = (filename or "").lower()
    if name.endswith(".csv"):
        for enc in ("utf-8", "utf-8-sig", "cp1255"):
            try:
                return pd.read_csv(io.BytesIO(content), encoding=enc, dtype=str)
            except UnicodeDecodeError:
                continue
        raise ValueError("לא הצלחנו לקרוא את ה-CSV — קידוד לא נתמך")

    if name.endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(content), dtype=str, engine="openpyxl")

    raise ValueError(f"סוג קובץ לא נתמך: {filename}")


def _pick_plan_column(df: pd.DataFrame) -> int:
    for idx, col in enumerate(df.columns):
        col_str = str(col).lower()
        if any(hint in col_str or hint in str(col) for hint in HEADER_HINTS):
            return idx

    best_idx = -1
    best_count = 0
    for idx, col in enumerate(df.columns):
        count = sum(
            1 for v in df[col].astype(str) if _normalize_to_entity_id(v) is not None
        )
        if count > best_count:
            best_count = count
            best_idx = idx

    if best_count == 0:
        raise ValueError(
            "לא זוהתה עמודת מספרי תכנית. ודא שיש עמודה בשם 'תכנית' "
            "או שהקובץ מכיל מספרי תכנית בפורמט 101-1234567 או 1234567"
        )

    return best_idx


def parse_plans_from_file(file_b64: str, filename: str) -> dict[str, str]:
    """Decode base64 file and return {entity_id: plan_name}."""
    content = base64.b64decode(file_b64)
    df = _read_dataframe(content, filename)
    if df.empty:
        raise ValueError("הקובץ ריק")

    plan_col = _pick_plan_column(df)
    name_col = plan_col + 1 if plan_col + 1 < len(df.columns) else None

    plans: dict[str, str] = {}
    for _, row in df.iterrows():
        eid = _normalize_to_entity_id(row.iloc[plan_col])
        if not eid:
            continue
        name = ""
        if name_col is not None:
            v = row.iloc[name_col]
            name = "" if pd.isna(v) else str(v).strip()
        plans[eid] = name

    if not plans:
        raise ValueError("לא נמצאו מספרי תכנית תקינים בקובץ")

    log.info("חולצו %d תכניות מהקובץ", len(plans))
    return plans


def _try_google_sheets(url: str) -> dict[str, str] | None:
    """If URL is a Google Sheets share link, fetch the CSV export and parse.
    Returns None if URL is not a Google Sheet."""
    m = GOOGLE_SHEETS_RE.search(url)
    if not m:
        return None
    sheet_id = m.group(1)
    csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    log.info("מזהה Google Sheets — מוריד כ-CSV: %s", csv_url)
    try:
        req = urllib.request.Request(
            csv_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read()
            content_type = resp.headers.get("Content-Type", "")
    except Exception as e:
        raise ValueError(
            f"לא הצלחנו לקרוא את ה-Google Sheet. ודא שהגישה מוגדרת "
            f"לקריאה לכל מי שיש לו את הקישור (Anyone with the link). שגיאה: {e}"
        )

    if "text/html" in content_type:
        raise ValueError(
            "ה-Google Sheet לא משותף לקריאה ציבורית. "
            "פתח את הקובץ → 'Share' → 'Anyone with the link can view'."
        )

    return parse_plans_from_file(
        base64.b64encode(content).decode(), f"{sheet_id}.csv"
    )


async def parse_plans_from_url(url: str) -> dict[str, str]:
    """Extract plan numbers from a URL.

    Special-cases Google Sheets (uses CSV export). Otherwise uses Playwright
    with domcontentloaded (avoids networkidle hang on dynamic sites)."""
    sheets_result = _try_google_sheets(url)
    if sheets_result is not None:
        log.info("חולצו %d תכניות מ-Google Sheets", len(sheets_result))
        return sheets_result

    log.info("טוען עמוד %s", url)
    plans: dict[str, str] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            await asyncio.sleep(3)
            html = await page.content()
        finally:
            await browser.close()

    for match in PLAN_PATTERN_FULL.finditer(html):
        token = match.group(0)
        if PHONE_PREFIX.match(token):
            continue
        suffix = token.split("-", 1)[1].lstrip("0") or "0"
        plans.setdefault(suffix, "")

    for match in re.finditer(r"\b\d{5,7}\b", html):
        token = match.group(0)
        eid = token.lstrip("0") or "0"
        plans.setdefault(eid, "")

    if not plans:
        raise ValueError("לא נמצאו מספרי תכנית בעמוד")

    log.info("חולצו %d תכניות מ-URL", len(plans))
    return plans
