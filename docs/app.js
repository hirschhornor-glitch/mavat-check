// Configure this to your deployed Cloudflare Worker URL.
const WORKER_URL = "https://mavat-check-dispatcher.YOUR-SUBDOMAIN.workers.dev/";

const MAX_FILE_BYTES = 40 * 1024;
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

const form = document.getElementById("form");
const fileInput = document.getElementById("file");
const urlInput = document.getElementById("url");
const emailInput = document.getElementById("email");
const submitBtn = document.getElementById("submit");
const statusEl = document.getElementById("status");

function showStatus(message, kind) {
  statusEl.className = kind;
  statusEl.textContent = message;
}

function clearStatus() {
  statusEl.className = "";
  statusEl.textContent = "";
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = reader.result;
      const comma = dataUrl.indexOf(",");
      resolve(comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl);
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  clearStatus();

  const email = emailInput.value.trim();
  const url = urlInput.value.trim();
  const file = fileInput.files[0] || null;

  if (!EMAIL_RE.test(email)) {
    showStatus("נא הזן כתובת מייל תקינה", "error");
    return;
  }

  if (!file && !url) {
    showStatus("נא העלה קובץ או הזן קישור", "error");
    return;
  }

  if (file && file.size > MAX_FILE_BYTES) {
    showStatus(
      `הקובץ גדול מדי (${(file.size / 1024).toFixed(1)}KB). מקסימום 40KB.`,
      "error",
    );
    return;
  }

  submitBtn.disabled = true;
  showStatus("שולח...", "info");

  try {
    let fileB64 = "";
    let fileName = "";

    if (file) {
      fileB64 = await readFileAsBase64(file);
      fileName = file.name;
    }

    const resp = await fetch(WORKER_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email,
        url: file ? "" : url,
        file_b64: fileB64,
        file_name: fileName,
      }),
    });

    const body = await resp.json().catch(() => ({}));

    if (resp.ok) {
      showStatus(
        "הבקשה התקבלה. סיכום יישלח אל " + email + " בעוד מספר דקות.",
        "success",
      );
      form.reset();
    } else {
      showStatus(
        "שגיאה: " + (body.error || `קוד ${resp.status}`),
        "error",
      );
    }
  } catch (err) {
    showStatus("שגיאת רשת: " + err.message, "error");
  } finally {
    submitBtn.disabled = false;
  }
});
