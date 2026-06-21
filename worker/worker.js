// Cloudflare Worker:
//   POST /             — accept form submission, optionally subscribe, dispatch GitHub Action
//   POST /unsubscribe  — remove a subscription by its token
//
// Required Worker secrets/vars:
//   GITHUB_PAT      — fine-grained PAT with Contents:Write on the repo (secret)
//   GITHUB_REPO     — "<owner>/mavat-check" (var or secret)
//   ALLOWED_ORIGIN  — full GitHub Pages origin, e.g. "https://user.github.io"

const MAX_FILE_B64_BYTES = 60_000;
const MAX_TOTAL_BYTES = 64_000;
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const SUBSCRIPTIONS_PATH = "subscriptions.json";

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

function resolveOrigin(request, env) {
  const allowed = (env.ALLOWED_ORIGIN || "").trim();
  const reqOrigin = request.headers.get("Origin") || "";
  return allowed && reqOrigin === allowed ? allowed : "";
}

export default {
  async fetch(request, env) {
    const origin = resolveOrigin(request, env);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(origin) });
    }

    if (request.method !== "POST") {
      return jsonResponse({ error: "Method not allowed" }, 405, origin);
    }

    if (!origin) {
      return jsonResponse({ error: "Origin not allowed" }, 403, "");
    }

    const url = new URL(request.url);
    if (url.pathname === "/unsubscribe") {
      return handleUnsubscribe(request, env, origin);
    }
    return handleSubmit(request, env, origin);
  },
};

async function handleSubmit(request, env, origin) {
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
    return jsonResponse({ error: "File too large (max ~40KB)" }, 413, origin);
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
      return jsonResponse({ error: "Invalid frequency" }, 400, origin);
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

  if (JSON.stringify(clientPayload).length > MAX_TOTAL_BYTES) {
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
    return jsonResponse({ status: "queued", subscribed: subscribe }, 200, origin);
  }

  const errorText = await ghResp.text().catch(() => "");
  return jsonResponse(
    {
      error: "GitHub dispatch failed",
      status: ghResp.status,
      detail: errorText.slice(0, 200),
    },
    502,
    origin,
  );
}

async function handleUnsubscribe(request, env, origin) {
  let payload;
  try {
    payload = await request.json();
  } catch {
    return jsonResponse({ error: "Invalid JSON" }, 400, origin);
  }

  const token = String(payload.token || "").trim();
  if (!token || token.length < 16) {
    return jsonResponse({ error: "Invalid token" }, 400, origin);
  }

  if (!env.GITHUB_PAT || !env.GITHUB_REPO) {
    return jsonResponse({ error: "Worker not configured" }, 500, origin);
  }

  try {
    const removed = await removeSubscription(env, token);
    if (!removed) {
      return jsonResponse({ error: "Token not found" }, 404, origin);
    }
    return jsonResponse({ status: "unsubscribed", email: removed.email }, 200, origin);
  } catch (e) {
    return jsonResponse(
      { error: "Unsubscribe failed", detail: String(e).slice(0, 200) },
      502,
      origin,
    );
  }
}

function ghHeaders(env) {
  return {
    "Authorization": `Bearer ${env.GITHUB_PAT}`,
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "mavat-check-worker",
  };
}

async function readSubscriptions(env) {
  const resp = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/contents/${SUBSCRIPTIONS_PATH}`,
    { headers: ghHeaders(env) },
  );
  if (!resp.ok) {
    throw new Error(`Read subscriptions failed: ${resp.status}`);
  }
  const fileMeta = await resp.json();
  const decoded = atob(fileMeta.content.replace(/\n/g, ""));
  let subs;
  try {
    subs = JSON.parse(decoded);
  } catch {
    subs = [];
  }
  if (!Array.isArray(subs)) subs = [];
  return { subs, sha: fileMeta.sha };
}

async function writeSubscriptions(env, subs, sha, message) {
  const newContent = JSON.stringify(subs, null, 2) + "\n";
  const newContentB64 = btoa(unescape(encodeURIComponent(newContent)));
  const resp = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/contents/${SUBSCRIPTIONS_PATH}`,
    {
      method: "PUT",
      headers: { ...ghHeaders(env), "Content-Type": "application/json" },
      body: JSON.stringify({ message, content: newContentB64, sha }),
    },
  );
  if (resp.ok) return true;
  if (resp.status === 409) return false;
  const errText = await resp.text().catch(() => "");
  throw new Error(`Write failed: ${resp.status} ${errText.slice(0, 100)}`);
}

async function addSubscription(env, { email, url, frequency }) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const { subs, sha } = await readSubscriptions(env);

    const idx = subs.findIndex(
      (s) =>
        (s.email || "").toLowerCase() === email.toLowerCase() &&
        (s.url || "") === url,
    );

    if (idx >= 0) {
      subs[idx].frequency = frequency;
      subs[idx].updated = new Date().toISOString();
      if (!subs[idx].token) subs[idx].token = crypto.randomUUID();
    } else {
      subs.push({
        email,
        url,
        frequency,
        token: crypto.randomUUID(),
        added: new Date().toISOString(),
      });
    }

    const ok = await writeSubscriptions(
      env,
      subs,
      sha,
      `Subscribe ${email} (${frequency})`,
    );
    if (ok) return;
  }
  throw new Error("Subscription write conflicts after retries");
}

async function removeSubscription(env, token) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const { subs, sha } = await readSubscriptions(env);
    const idx = subs.findIndex((s) => s.token === token);
    if (idx < 0) return null;

    const removed = subs[idx];
    subs.splice(idx, 1);

    const ok = await writeSubscriptions(
      env,
      subs,
      sha,
      `Unsubscribe ${removed.email}`,
    );
    if (ok) return removed;
  }
  throw new Error("Unsubscribe write conflicts after retries");
}
