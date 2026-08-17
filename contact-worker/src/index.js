const NOTIFY_ADDRESS = "thabhelo@deepubuntu.com";
const FROM_ADDRESS = "contact@fraeno.com";
const ALLOWED_ORIGINS = new Set(["https://fraeno.com", "https://www.fraeno.com"]);
const MAX_BODY_BYTES = 8192;
const MINIMUM_DWELL_MS = 3000;
const MAX_ADMIN_LEADS = 1000;
const FIREBASE_JWKS_URL =
  "https://www.googleapis.com/service_accounts/v1/jwk/" +
  "securetoken@system.gserviceaccount.com";

const FIELD_LIMITS = {
  name: { min: 1, max: 120 },
  email: { min: 5, max: 254 },
  company: { min: 0, max: 200 },
  message: { min: 0, max: 4000 },
};

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

function reject(status, reason) {
  return new Response(JSON.stringify({ ok: false, reason }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
    },
  });
}

function decodeBase64Url(value) {
  const padded = value.replaceAll("-", "+").replaceAll("_", "/").padEnd(
    Math.ceil(value.length / 4) * 4,
    "="
  );
  const binary = atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function jwtObject(value) {
  const decoded = new TextDecoder().decode(decodeBase64Url(value));
  const parsed = JSON.parse(decoded);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("JWT section is not an object");
  }
  return parsed;
}

async function verifyFirebaseAdminToken(token, env) {
  const parts = token.split(".");
  if (parts.length !== 3) {
    return false;
  }
  let header;
  let payload;
  try {
    header = jwtObject(parts[0]);
    payload = jwtObject(parts[1]);
  } catch {
    return false;
  }
  if (
    header.alg !== "RS256" ||
    typeof header.kid !== "string" ||
    !header.kid ||
    typeof env.FIREBASE_PROJECT_ID !== "string" ||
    !env.FIREBASE_PROJECT_ID
  ) {
    return false;
  }
  let jwks;
  try {
    const response = await fetch(FIREBASE_JWKS_URL, {
      cf: { cacheEverything: true, cacheTtl: 3600 },
    });
    if (!response.ok) {
      return false;
    }
    jwks = await response.json();
  } catch {
    return false;
  }
  const keys = Array.isArray(jwks.keys) ? jwks.keys : [];
  const jwk = keys.find((candidate) => candidate.kid === header.kid);
  if (!jwk) {
    return false;
  }
  let verified = false;
  try {
    const key = await crypto.subtle.importKey(
      "jwk",
      jwk,
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["verify"]
    );
    verified = await crypto.subtle.verify(
      "RSASSA-PKCS1-v1_5",
      key,
      decodeBase64Url(parts[2]),
      new TextEncoder().encode(`${parts[0]}.${parts[1]}`)
    );
  } catch {
    return false;
  }
  const now = Math.floor(Date.now() / 1000);
  return (
    verified &&
    payload.aud === env.FIREBASE_PROJECT_ID &&
    payload.iss === `https://securetoken.google.com/${env.FIREBASE_PROJECT_ID}` &&
    typeof payload.sub === "string" &&
    payload.sub.length > 0 &&
    payload.sub.length <= 128 &&
    typeof payload.exp === "number" &&
    payload.exp > now &&
    typeof payload.iat === "number" &&
    payload.iat <= now + 300 &&
    typeof payload.auth_time === "number" &&
    payload.auth_time <= now + 300 &&
    payload.isAdmin === true
  );
}

async function handleAdminLeads(request, env) {
  const authorization = request.headers.get("Authorization") || "";
  const token = authorization.startsWith("Bearer ")
    ? authorization.slice("Bearer ".length).trim()
    : "";
  if (!token || !(await verifyFirebaseAdminToken(token, env))) {
    return reject(401, "admin authentication is required");
  }
  const leads = [];
  let cursor;
  do {
    const result = await env.CONTACTS.list({
      limit: Math.min(1000, MAX_ADMIN_LEADS - leads.length),
      ...(cursor ? { cursor } : {}),
    });
    const records = await Promise.all(
      result.keys.map((item) => env.CONTACTS.get(item.name, "json"))
    );
    records.forEach((record) => {
      if (record && leads.length < MAX_ADMIN_LEADS) {
        leads.push({
          name: record.name || "",
          email: record.email || "",
          company: record.company || "",
          last_message: record.last_message || "",
          updates: record.updates === true,
          first_seen: record.first_seen || "",
          last_seen: record.last_seen || "",
          submissions: Number(record.submissions || 0),
        });
      }
    });
    cursor = result.list_complete ? undefined : result.cursor;
  } while (cursor && leads.length < MAX_ADMIN_LEADS);
  return jsonResponse({ ok: true, leads });
}

const EMAIL_FONT =
  "'Inter Tight', 'Helvetica Neue', Helvetica, Arial, sans-serif";

function emailShell(heading, bodyHtml, footerLine) {
  return (
    '<div style="margin:0;padding:36px 16px;background-color:#f3f2ef;">' +
    "<style>@font-face{font-family:'Inter Tight';" +
    "src:url('https://fraeno.com/assets/inter-tight-latin.woff2') format('woff2');" +
    "font-weight:400 700;font-display:swap;}</style>" +
    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto;">' +
    '<tr><td style="padding:0 6px 16px;">' +
    `<span style="font-family:${EMAIL_FONT};font-size:22px;font-weight:600;letter-spacing:-0.01em;color:#151515;">fraeno</span>` +
    "</td></tr>" +
    '<tr><td style="background-color:#ffffff;border:1px solid rgba(21,21,21,0.13);border-radius:16px;padding:36px 32px 32px;">' +
    `<h1 style="margin:0 0 6px;font-family:${EMAIL_FONT};font-size:24px;font-weight:600;line-height:1.25;letter-spacing:-0.015em;color:#151515;">${heading}</h1>` +
    bodyHtml +
    "</td></tr>" +
    '<tr><td style="padding:24px 6px 0;text-align:center;">' +
    `<p style="margin:0 0 8px;font-family:${EMAIL_FONT};font-size:13px;line-height:1.6;color:#92918d;">${footerLine}</p>` +
    `<p style="margin:0;font-family:${EMAIL_FONT};font-size:13px;line-height:1.6;color:#92918d;">` +
    "&copy; 2026 DeepUbuntu Labs &nbsp;&middot;&nbsp; San Francisco, CA &nbsp;&middot;&nbsp; " +
    '<a href="https://fraeno.com/" style="color:#92918d;">fraeno.com</a>' +
    "</p></td></tr>" +
    "</table></div>"
  );
}

function emailParagraph(text) {
  return (
    `<p style="margin:16px 0 0;font-family:${EMAIL_FONT};font-size:16px;line-height:1.7;color:#151515;">` +
    text +
    "</p>"
  );
}

function emailButton(label, href) {
  return (
    '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:26px 0 2px;"><tr>' +
    '<td style="background-color:#080808;border-radius:999px;">' +
    `<a href="${href}" style="display:inline-block;padding:13px 28px;font-family:${EMAIL_FONT};font-size:15px;font-weight:500;color:#ffffff;text-decoration:none;">${label}</a>` +
    "</td></tr></table>"
  );
}

function validate(payload) {
  for (const [field, limits] of Object.entries(FIELD_LIMITS)) {
    const value = payload[field];
    if (typeof value !== "string") {
      if (limits.min === 0 && value === undefined) {
        continue;
      }
      return `${field} is required`;
    }
    const trimmed = value.trim();
    if (trimmed.length < limits.min || trimmed.length > limits.max) {
      return `${field} must be between ${limits.min} and ${limits.max} characters`;
    }
  }
  if (!EMAIL_PATTERN.test(payload.email.trim())) {
    return "email must be a valid address";
  }
  return null;
}

function unsubscribePage(heading, body) {
  return new Response(
    '<!doctype html><html lang="en"><head><meta charset="utf-8" />' +
      '<meta name="viewport" content="width=device-width, initial-scale=1" />' +
      "<title>Fraeno | Unsubscribe</title></head>" +
      '<body style="margin:0;padding:64px 16px;background-color:#f3f2ef;">' +
      '<div style="max-width:560px;margin:0 auto;">' +
      `<p style="font-family:${EMAIL_FONT};font-size:22px;font-weight:600;color:#151515;margin:0 0 24px;">fraeno</p>` +
      '<div style="background-color:#ffffff;border:1px solid rgba(21,21,21,0.13);border-radius:16px;padding:36px 32px;">' +
      `<h1 style="font-family:${EMAIL_FONT};font-size:24px;font-weight:600;color:#151515;margin:0 0 12px;">${heading}</h1>` +
      `<p style="font-family:${EMAIL_FONT};font-size:16px;line-height:1.7;color:#151515;margin:0;">${body}</p>` +
      "</div>" +
      `<p style="font-family:${EMAIL_FONT};font-size:13px;color:#92918d;margin:20px 0 0;text-align:center;">` +
      'DeepUbuntu Labs &nbsp;&middot;&nbsp; <a href="https://fraeno.com/" style="color:#92918d;">fraeno.com</a></p>' +
      "</div></body></html>",
    {
      status: heading === "You are unsubscribed" ? 200 : 404,
      headers: { "Content-Type": "text/html; charset=utf-8" },
    }
  );
}

async function handleUnsubscribe(request, env) {
  const url = new URL(request.url);
  const email = (url.searchParams.get("e") || "").trim().toLowerCase();
  const token = (url.searchParams.get("t") || "").trim();
  const record = email ? await env.CONTACTS.get(email, "json") : null;
  const valid =
    record !== null &&
    typeof record.unsubscribe_token === "string" &&
    record.unsubscribe_token.length > 0 &&
    record.unsubscribe_token === token;
  if (!valid) {
    if (request.method === "POST") {
      return new Response(null, { status: 404 });
    }
    return unsubscribePage(
      "This link is not valid",
      "The unsubscribe link is incomplete or expired. Email " +
        '<a href="mailto:thabhelo@deepubuntu.com" style="color:#ff6333;">thabhelo@deepubuntu.com</a>' +
        " and we will remove you right away."
    );
  }
  await env.CONTACTS.put(
    email,
    JSON.stringify({
      ...record,
      updates: false,
      unsubscribed_at: new Date().toISOString(),
    })
  );
  if (request.method === "POST") {
    return new Response(null, { status: 200 });
  }
  return unsubscribePage(
    "You are unsubscribed",
    "You will not receive product updates from Fraeno. If this was a " +
      "mistake, just tick the updates box next time you write to us."
  );
}

export default {
  async fetch(request, env) {
    const pathname = new URL(request.url).pathname;
    if (pathname === "/api/admin/config") {
      if (request.method !== "GET") {
        return reject(405, "only GET is accepted");
      }
      if (!env.FIREBASE_API_KEY || !env.FIREBASE_PROJECT_ID) {
        return reject(503, "admin authentication is not configured");
      }
      return jsonResponse({
        apiKey: env.FIREBASE_API_KEY,
        authDomain:
          env.FIREBASE_AUTH_DOMAIN || `${env.FIREBASE_PROJECT_ID}.firebaseapp.com`,
        projectId: env.FIREBASE_PROJECT_ID,
      });
    }
    if (pathname === "/api/admin/leads") {
      if (request.method !== "GET") {
        return reject(405, "only GET is accepted");
      }
      return handleAdminLeads(request, env);
    }
    if (pathname === "/api/unsubscribe") {
      if (request.method !== "GET" && request.method !== "POST") {
        return reject(405, "only GET and POST are accepted");
      }
      return handleUnsubscribe(request, env);
    }
    if (request.method !== "POST") {
      return reject(405, "only POST is accepted");
    }
    const origin = request.headers.get("Origin") || "";
    if (!ALLOWED_ORIGINS.has(origin)) {
      return reject(403, "the request origin is not the Fraeno site");
    }
    const length = Number(request.headers.get("Content-Length") || "0");
    if (length > MAX_BODY_BYTES) {
      return reject(413, "the request body is too large");
    }

    let payload;
    try {
      payload = await request.json();
    } catch {
      return reject(400, "the request body must be JSON");
    }
    if (typeof payload !== "object" || payload === null) {
      return reject(400, "the request body must be an object");
    }

    if (typeof payload.website === "string" && payload.website.trim() !== "") {
      return reject(400, "the submission failed validation");
    }
    const dwell = Number(payload.dwell_ms);
    if (!Number.isFinite(dwell) || dwell < MINIMUM_DWELL_MS) {
      return reject(400, "the submission failed validation");
    }

    const invalid = validate(payload);
    if (invalid !== null) {
      return reject(400, invalid);
    }

    const name = payload.name.trim();
    const email = payload.email.trim();
    const company = (payload.company || "").trim();
    const message = (payload.message || "").trim();
    const wantsUpdates = payload.updates === true;

    const key = email.toLowerCase();
    const existing = await env.CONTACTS.get(key, "json");
    const unsubscribeToken =
      existing && typeof existing.unsubscribe_token === "string"
        ? existing.unsubscribe_token
        : crypto.randomUUID();
    const unsubscribeUrl =
      "https://fraeno.com/api/unsubscribe?e=" +
      encodeURIComponent(key) +
      "&t=" +
      encodeURIComponent(unsubscribeToken);
    await env.CONTACTS.put(
      key,
      JSON.stringify({
        name,
        email,
        company,
        unsubscribe_token: unsubscribeToken,
        updates: wantsUpdates || (existing ? existing.updates === true : false),
        last_message: message,
        submissions: existing ? (existing.submissions || 1) + 1 : 1,
        first_seen: existing ? existing.first_seen : new Date().toISOString(),
        last_seen: new Date().toISOString(),
      })
    );
    try {
      const confirmationBody =
        emailParagraph("Hi there,") +
        emailParagraph(
          "Thanks for reaching out. Your request has been received and we " +
            "read every single one."
        ) +
        emailParagraph(
          "Fraeno is built by a small team on a simple belief that " +
            "robots can be dangerous, even deadly, and we want to make sure " +
            "bad code doesn't accidentally cause that. Conversations like " +
            "yours shape what we build next, so we genuinely look forward " +
            "to talking to you."
        ) +
        emailParagraph(
          "You will hear from us shortly. If you would like to talk sooner, " +
            "please pick a time that works for you and we will meet you " +
            "there!"
        ) +
        emailButton(
          "Book a call",
          "https://calendar.app.google/fB6AtdB5FVSs8YoA9"
        ) +
        emailParagraph("Talk soon,<br>Thabhelo");
      await env.EMAIL.send({
        to: email,
        from: { email: FROM_ADDRESS, name: "Fraeno" },
        replyTo: { email: NOTIFY_ADDRESS, name: "Thabhelo Duve" },
        subject: "We received your Fraeno access request",
        text:
          "Hi there,\n\n" +
          "Thanks for reaching out. Your request has been received and we " +
          "read every single one.\n\n" +
          "Fraeno is built by a small team on a simple belief that " +
          "robots can be dangerous, even deadly, and we want to make sure " +
          "bad code doesn't accidentally cause that. Conversations like " +
          "yours shape what we build next, so we genuinely look forward " +
          "to talking to you.\n\n" +
          "You will hear from us shortly. If you would like to talk sooner, " +
          "please pick a time that works for you and we will meet you " +
          "there! Book a call: " +
          "https://calendar.app.google/fB6AtdB5FVSs8YoA9\n\n" +
          "Talk soon,\nThabhelo\n",
        headers: {
          "List-Unsubscribe": `<${unsubscribeUrl}>`,
          "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
        html: emailShell(
          "We got your request",
          confirmationBody,
          "You are receiving this one-time message because you requested " +
            "access at fraeno.com.<br>" +
            `<a href="${unsubscribeUrl}" style="display:inline-block;` +
            'padding:12px 16px;font-size:14px;color:#666663;">' +
            "Unsubscribe from product updates</a>"
        ),
      });
    } catch (error) {
      console.error(
        JSON.stringify({
          message: "confirmation send failed",
          error: error instanceof Error ? error.message : String(error),
        })
      );
    }

    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  },
};
