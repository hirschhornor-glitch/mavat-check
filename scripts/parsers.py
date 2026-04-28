"""Parse plan numbers from uploaded files (xlsx/csv) or arbitrary URLs."""

import asyncio
import base64
import io
import logging
import re

import pandas as pd
from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

PLAN_PATTERN_FULL = re.compile(r"\d{3}-\d{7}")
PLAN_PATTERN_ENTITY = re.compile(r"^\d{5,7}$")
PHONE_PREFIX = re.compile(r"^(05[0-9]|07[2-7])-")

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


async def parse_plans_from_url(url: str) -> dict[str, str]:
    """Scrape arbitrary URL with Playwright and extract plan numbers via regex."""
    log.info("טוען עמוד %s", url)
    plans: dict[str, str] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=60_000)
            await asyncio.sleep(2)
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
