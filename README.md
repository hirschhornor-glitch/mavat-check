# MavatCheck

עמוד אינטרנט שמאפשר לבדוק האם תכניות מרשימה (קובץ או URL) עולות לסדרי היום של הוועדה המחוזית ירושלים ב-14 הימים הקרובים. הסיכום נשלח במייל.

## ארכיטקטורה

```
[GitHub Pages: docs/]  --POST-->  [Cloudflare Worker]  --repository_dispatch-->  [GitHub Actions]
                                                                                         |
                                                                                         v
                                                                              [Playwright + Mavat]
                                                                                         |
                                                                                         v
                                                                              [SMTP -> user email]
```

## הגדרה חד-פעמית

### 1. חשבון Gmail
- צור חשבון `mavatcheck@gmail.com` (או אחר), הפעל 2FA
- הפק App Password ב-https://myaccount.google.com/apppasswords
- שמור את הסיסמה (16 תווים) — לא תוצג שוב

### 2. GitHub repo
- צור repo בשם `mavat-check` (פרטי או ציבורי)
- Push את הקוד הזה
- ב-Settings → Pages: Branch `main`, תיקייה `/docs`
- ב-Settings → Secrets and variables → Actions, הוסף:
  - `MAVATCHECK_GMAIL_USER` = `mavatcheck@gmail.com`
  - `MAVATCHECK_GMAIL_APP_PASSWORD` = הסיסמה (16 תווים, ללא רווחים)

### 3. Cloudflare Worker
```bash
cd worker
npm install
npx wrangler login
npx wrangler deploy
```
לאחר ה-deploy:
```bash
npx wrangler secret put GITHUB_PAT          # PAT עם Contents:Write על mavat-check
npx wrangler secret put GITHUB_REPO         # למשל "your-username/mavat-check"
npx wrangler secret put ALLOWED_ORIGIN      # למשל "https://your-username.github.io"
```

צור PAT ב-https://github.com/settings/personal-access-tokens/new עם:
- Repository access: רק `mavat-check`
- Permissions → Repository → **Contents: Read and write**

### 4. Frontend config
ערוך את `docs/app.js` שורה 2:
```js
const WORKER_URL = "https://mavat-check-dispatcher.YOUR-SUBDOMAIN.workers.dev/";
```
החלף ב-URL שקיבלת מ-`wrangler deploy`.

Push ו-Pages יתעדכן תוך כמה דקות.

## בדיקה

### מקומית — `scripts/check.py`
```bash
pip install -r requirements.txt
python -m playwright install chromium

export MAVATCHECK_GMAIL_USER=mavatcheck@gmail.com
export MAVATCHECK_GMAIL_APP_PASSWORD=...

# קובץ Excel
python scripts/check.py --email you@example.com \
  --file-b64 "$(base64 -w0 sample.xlsx)" \
  --file-name sample.xlsx

# או URL
python scripts/check.py --email you@example.com \
  --url https://armon-status-buddy.lovable.app/plans
```

### GitHub Action ידני
ב-Actions tab → MavatCheck → "Run workflow" → מלא email + url או file_b64.

### Worker מקומית
```bash
cd worker
npx wrangler dev
# בטרמינל אחר:
curl -X POST http://localhost:8787 \
  -H "Origin: https://your-username.github.io" \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","url":"https://example.com","file_b64":"","file_name":""}'
```

## מבנה הקוד

```
mavat-check/
├── docs/                    # GitHub Pages (frontend)
│   ├── index.html
│   ├── styles.css
│   └── app.js
├── worker/                  # Cloudflare Worker
│   ├── worker.js
│   └── wrangler.toml
├── scripts/                 # GitHub Actions runtime
│   ├── check.py             # Entry point
│   ├── parsers.py           # File / URL → list of plan numbers
│   ├── mavat.py             # Mavat scraping
│   └── mailer.py            # SMTP
├── .github/workflows/
│   └── check.yml
└── requirements.txt
```

## פורמטים נתמכים

- **Excel/CSV**: עמודה עם מספרי תכנית בפורמט `101-1234567` או entity ID `1234567`. אם יש header המכיל "תכנית"/"plan"/"agam"/"entity" — תיבחר אוטומטית. אחרת תיבחר העמודה עם הכי הרבה מספרי תכנית תקינים. שם תכנית נלקח מהעמודה הסמוכה אם קיימת.
- **URL**: טעינת העמוד עם Playwright וחיפוש regex של `\d{3}-\d{7}` או `\d{5,7}`.

## מגבלות

- גודל קובץ עד 40KB (מגבלת `client_payload` של GitHub היא 64KB אחרי base64).
- חלון זמן 14 יום קדימה בלבד.
- רק הוועדה המחוזית ירושלים.
- ריצה אד-הוק בלבד — אין tracking לכפילויות בין ריצות.
