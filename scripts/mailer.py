"""Parametric SMTP mailer — sends from mavatcheck@gmail.com to the user."""

import logging
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def _sender() -> tuple[str, str]:
    user = os.environ.get("MAVATCHECK_GMAIL_USER", "").strip()
    pwd = os.environ.get("MAVATCHECK_GMAIL_APP_PASSWORD", "").strip()
    if not user or not pwd:
        raise RuntimeError(
            "MAVATCHECK_GMAIL_USER / MAVATCHECK_GMAIL_APP_PASSWORD לא מוגדרים"
        )
    return user, pwd


def _wrap_html(body_inner: str) -> str:
    return (
        '<html><body dir="rtl" style="font-family:Arial,sans-serif;">'
        f"{body_inner}"
        '<p style="color:#888;font-size:12px;margin-top:24px;">'
        "הודעה זו נשלחה אוטומטית ע\"י MavatCheck"
        "</p></body></html>"
    )


SOURCE_LABELS = {
    "mavat": "ועדה מחוזית (Mavat)",
    "city": "ועדה מקומית (עירייה)",
}


def _matches_table(matches: list[dict]) -> str:
    rows = ""
    for m in matches:
        detail_url = m.get("detail_url", "")
        link_html = f'<a href="{detail_url}">קישור</a>' if detail_url else "-"
        source_label = SOURCE_LABELS.get(m.get("source", "mavat"), m.get("source", ""))
        rows += (
            "<tr>"
            f'<td style="padding:8px;border:1px solid #ddd;">{source_label}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{m["plan"]}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{m.get("plan_name", "")}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{m["meeting_title"]}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{m["meeting_date"]}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{m.get("meeting_time", "")}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{link_html}</td>'
            "</tr>"
        )
    return (
        '<table style="border-collapse:collapse;width:100%">'
        '<tr style="background:#f2f2f2">'
        '<th style="padding:8px;border:1px solid #ddd;">מקור</th>'
        '<th style="padding:8px;border:1px solid #ddd;">מספר תכנית</th>'
        '<th style="padding:8px;border:1px solid #ddd;">שם התכנית</th>'
        '<th style="padding:8px;border:1px solid #ddd;">ישיבה</th>'
        '<th style="padding:8px;border:1px solid #ddd;">תאריך</th>'
        '<th style="padding:8px;border:1px solid #ddd;">שעה</th>'
        '<th style="padding:8px;border:1px solid #ddd;">קישור</th>'
        f"</tr>{rows}</table>"
    )


def send_results_email(
    recipient: str,
    matches: list[dict],
    plans_count: int,
    partial_errors: list[str] | None = None,
) -> None:
    sender, password = _sender()
    now = datetime.now().strftime("%d/%m/%Y %H:%M")

    errors_html = ""
    if partial_errors:
        errors_list = "".join(f"<li>{e}</li>" for e in partial_errors)
        errors_html = (
            '<p style="color:#b91c1c;">'
            "חלק מהמקורות נכשלו (התוצאות חלקיות):"
            f"<ul>{errors_list}</ul></p>"
        )

    if matches:
        subject = (
            f"MavatCheck: נמצאו {len(matches)} התאמות בוועדות תכנון ירושלים "
            f"– {datetime.now().strftime('%d/%m/%Y')}"
        )
        body_inner = (
            f"<h2>נמצאו {len(matches)} התאמות בוועדות תכנון ירושלים</h2>"
            f"<p>בבדיקה שבוצעה ב-{now} על {plans_count} תכניות "
            "(ועדה מחוזית — Mavat ו-ועדה מקומית — אתר העירייה):</p>"
            f"{errors_html}{_matches_table(matches)}"
        )
    else:
        subject = (
            f"MavatCheck: לא נמצאו התאמות – {datetime.now().strftime('%d/%m/%Y')}"
        )
        body_inner = (
            "<h2>הבדיקה הסתיימה — לא נמצאו התאמות</h2>"
            f"<p>בבדיקה שבוצעה ב-{now} על {plans_count} תכניות, "
            "לא נמצאה אף תכנית בסדרי היום של הוועדה המחוזית ירושלים (Mavat) "
            "או הוועדה המקומית של עיריית ירושלים בחלון 14 יום אחורה / 30 יום קדימה.</p>"
            f"{errors_html}"
        )

    _send(sender, password, recipient, subject, _wrap_html(body_inner))


def send_error_email(recipient: str, error_message: str) -> None:
    sender, password = _sender()
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    subject = f"MavatCheck: שגיאה בבדיקה – {datetime.now().strftime('%d/%m/%Y')}"
    body_inner = (
        "<h2>אירעה שגיאה בעת ביצוע הבדיקה</h2>"
        f"<p>בבדיקה שבוצעה ב-{now} אירעה השגיאה הבאה:</p>"
        f'<pre style="background:#fee;padding:12px;border:1px solid #fbb;'
        f'white-space:pre-wrap;">{error_message}</pre>'
        "<p>נסה שוב או צור קשר עם תמיכה.</p>"
    )
    _send(sender, password, recipient, subject, _wrap_html(body_inner))


def _send(sender: str, password: str, recipient: str, subject: str, html: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())
    log.info("מייל נשלח אל %s", recipient)
