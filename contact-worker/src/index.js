const NOTIFY_ADDRESS = "thabhelo@deepubuntu.com";
const FROM_ADDRESS = "contact@fraeno.com";
const ALLOWED_ORIGINS = new Set(["https://fraeno.com", "https://www.fraeno.com"]);
const MAX_BODY_BYTES = 8192;
const MINIMUM_DWELL_MS = 3000;

const FIELD_LIMITS = {
  name: { min: 1, max: 120 },
  email: { min: 5, max: 254 },
  company: { min: 0, max: 200 },
  message: { min: 10, max: 4000 },
};

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

function reject(status, reason) {
  return new Response(JSON.stringify({ ok: false, reason }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
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

export default {
  async fetch(request, env) {
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
    const message = payload.message.trim();
    const subject = `Fraeno access request from ${name}`;
    const lines = [
      `Name: ${name}`,
      `Email: ${email}`,
      company ? `Company: ${company}` : null,
      "",
      message,
    ].filter((line) => line !== null);

    try {
      await env.EMAIL.send({
        to: NOTIFY_ADDRESS,
        from: { email: FROM_ADDRESS, name: "Fraeno website" },
        replyTo: { email, name },
        subject,
        text: lines.join("\n"),
        html: [
          `<p><strong>Name:</strong> ${escapeHtml(name)}</p>`,
          `<p><strong>Email:</strong> ${escapeHtml(email)}</p>`,
          company ? `<p><strong>Company:</strong> ${escapeHtml(company)}</p>` : "",
          `<p>${escapeHtml(message).replaceAll("\n", "<br>")}</p>`,
        ].join(""),
      });
    } catch (error) {
      console.log(`contact send failed: ${error}`);
      return reject(502, "the message could not be delivered; email thabhelo@deepubuntu.com directly");
    }

    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  },
};
