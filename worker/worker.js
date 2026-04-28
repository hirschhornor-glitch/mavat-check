// Cloudflare Worker: receives form submission from GitHub Pages frontend,
// validates input, and triggers a repository_dispatch event on the
// mavat-check GitHub repo. The PAT is stored as a Worker secret.
//
// Required Worker secrets/vars:
//   GITHUB_PAT       — fine-grained PAT with Contents:Write on the repo (secret)
//   GITHUB_REPO      — "<owner>/mavat-check" (var or secret)
//   ALLOWED_ORIGIN   — full GitHub Pages origin, e.g. "https://user.github.io"

const MAX_FILE_B64_BYTES = 60_000;
const MAX_TOTAL_BYTES = 64_000;
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

function corsHeaders(origin) {
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
  };
}

function jsonResponse(body, status, origin) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...corsHeaders(origin),
    },
  });
}

export default {
  async fetch(request, env) {
    const allowed = (env.ALLOWED_ORIGIN || "").trim();
    const reqOrigin = request.headers.get("Origin") || "";
    const origin = allowed && reqOrigin === allowed ? allowed : "";

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(origin) });
    }

    if (request.method !== "POST") {
      return jsonResponse({ error: "Method not allowed" }, 405, origin);
    }

    if (!origin) {
      return jsonResponse({ error: "Origin not allowed" }, 403, "");
    }

    let payload;
    try {
      payload = await request.json();
    } catch {
      return jsonResponse({ error: "Invalid JSON" }, 400, origin);
    }

    const email = String(payload.email || "").trim();
    const url = String(payload.url || "").trim();
    const fileB64 = String(payload.file_b64 || "");
    const fileName = String(payload.file_name || "").trim();

    if (!EMAIL_RE.test(email)) {
      return jsonResponse({ error: "Invalid email" }, 400, origin);
    }
    if (!fileB64 && !url) {
      return jsonResponse({ error: "File or URL is required" }, 400, origin);
    }
    if (fileB64.length > MAX_FILE_B64_BYTES) {
      return jsonResponse(
        { error: "File too large (max ~40KB)" },
        413,
        origin,
      );
    }

    const subscribe = Boolean(payload.subscribe);
    const frequency = String(payload.frequency || "").trim();
    if (subscribe) {
      if (!url) {
        return jsonResponse(
          { error: "Subscription requires a URL (cannot reschedule a file)" },
          400,
          origin,
        );
      }
      if (frequency !== "daily" && frequency !== "weekly") {
        return jsonResponse(
          { error: "Invalid frequency" },
          400,
          origin,
        );
      }
      try {
        await addSubscription(env, { email, url, frequency });
      } catch (e) {
        return jsonResponse(
          { error: "Subscription failed", detail: String(e).slice(0, 200) },
          502,
          origin,
        );
      }
    }

    const clientPayload = {
      email,
      url,
      file_b64: fileB64,
      file_name: fileName,
      submitted_at: new Date().toISOString(),
    };

    const totalSize = JSON.stringify(clientPayload).length;
    if (totalSize > MAX_TOTAL_BYTES) {
      return jsonResponse({ error: "Payload too large" }, 413, origin);
    }

    if (!env.GITHUB_PAT || !env.GITHUB_REPO) {
      return jsonResponse({ error: "Worker not configured" }, 500, origin);
    }

    const ghResp = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`,
      {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${env.GITHUB_PAT}`,
          "Accept": "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "Content-Type": "application/json",
          "User-Agent": "mavat-check-worker",
        },
        body: JSON.stringify({
          event_type: "mavat-check",
          client_payload: clientPayload,
        }),
      },
    );

    if (ghResp.status === 204) {
      return jsonResponse(
        { status: "queued", subscribed: subscribe },
        200,
        origin,
      );
    }

    const errorText = await ghResp.text().catch(() => "");
    return jsonResponse(
      { error: "GitHub dispatch failed", status: ghResp.status, detail: errorText.slice(0, 200) },
      502,
      origin,
    );
  },
};

const SUBSCRIPTIONS_PATH = "subscriptions.json";

async function addSubscription(env, { email, url, frequency }) {
  const ghHeaders = {
    "Authorization": `Bearer ${env.GITHUB_PAT}`,
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "mavat-check-worker",
  };

  for (let attempt = 0; attempt < 3; attempt++) {
    const getResp = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/contents/${SUBSCRIPTIONS_PATH}`,
      { headers: ghHeaders },
    );
    if (!getResp.ok) {
      throw new Error(`Read subscriptions failed: ${getResp.status}`);
    }
    const fileMeta = await getResp.json();
    const sha = fileMeta.sha;
    const decoded = atob(fileMeta.content.replace(/\n/g, ""));
    let subs;
    try {
      subs = JSON.parse(decoded);
    } catch {
      subs = [];
    }
    if (!Array.isArray(subs)) subs = [];

    const exists = subs.some(
      (s) =>
        (s.email || "").toLowerCase() === email.toLowerCase() &&
        (s.url || "") === url,
    );
    if (exists) {
      const idx = subs.findIndex(
        (s) =>
          (s.email || "").toLowerCase() === email.toLowerCase() &&
          (s.url || "") === url,
      );
      subs[idx].frequency = frequency;
      subs[idx].updated = new Date().toISOString();
    } else {
      subs.push({
        email,
        url,
        frequency,
        added: new Date().toISOString(),
      });
    }

    const newContent = JSON.stringify(subs, null, 2) + "\n";
    const newContentB64 = btoa(unescape(encodeURIComponent(newContent)));

    const putResp = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/contents/${SUBSCRIPTIONS_PATH}`,
      {
        method: "PUT",
        headers: { ...ghHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({
          message: `Subscribe ${email} (${frequency})`,
          content: newContentB64,
          sha,
        }),
      },
    );
    if (putResp.ok) return;
    if (putResp.status === 409) continue;
    const errText = await putResp.text().catch(() => "");
    throw new Error(`Write subscriptions failed: ${putResp.status} ${errText.slice(0, 100)}`);
  }
  throw new Error("Subscription write conflicts after retries");
}
