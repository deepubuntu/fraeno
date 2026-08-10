const NOTIFY_ADDRESS = "thabhelo@deepubuntu.com";
const FROM_ADDRESS = "contact@fraeno.com";
const ALLOWED_ORIGINS = new Set(["https://fraeno.com", "https://www.fraeno.com"]);
const MAX_BODY_BYTES = 8192;
const MINIMUM_DWELL_MS = 3000;

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
    "DeepUbuntu Labs &nbsp;&middot;&nbsp; San Francisco, CA &nbsp;&middot;&nbsp; " +
    '<a href="https://fraeno.com/" style="color:#92918d;">fraeno.com</a>' +
    "</p></td></tr>" +
    "</table></div>"
  );
}

function emailRow(label, value) {
  return (
    `<p style="margin:0 0 8px;font-family:${EMAIL_FONT};font-size:15px;line-height:1.6;color:#151515;">` +
    `<span style="color:#666663;">${label}</span>&nbsp;&nbsp;${value}</p>`
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
    const message = (payload.message || "").trim();
    const wantsUpdates = payload.updates === true;

    const key = email.toLowerCase();
    const existing = await env.CONTACTS.get(key, "json");
    await env.CONTACTS.put(
      key,
      JSON.stringify({
        name,
        email,
        company,
        updates: wantsUpdates || (existing ? existing.updates === true : false),
        last_message: message,
        submissions: existing ? (existing.submissions || 1) + 1 : 1,
        first_seen: existing ? existing.first_seen : new Date().toISOString(),
        last_seen: new Date().toISOString(),
      })
    );
    const subject = `Fraeno access request from ${name}`;
    const lines = [
      `Name: ${name}`,
      `Email: ${email}`,
      company ? `Company: ${company}` : null,
      wantsUpdates ? "Opted into product updates" : null,
      message ? "" : null,
      message || null,
    ].filter((line) => line !== null);

    const notificationBody =
      emailRow("Name", escapeHtml(name)) +
      emailRow("Email", escapeHtml(email)) +
      (company ? emailRow("Company", escapeHtml(company)) : "") +
      (wantsUpdates ? emailRow("Updates", "Opted in") : "") +
      (message
        ? emailParagraph(escapeHtml(message).replaceAll("\n", "<br>"))
        : "");
    try {
      await env.EMAIL.send({
        to: NOTIFY_ADDRESS,
        from: { email: FROM_ADDRESS, name: "Fraeno website" },
        replyTo: { email, name },
        subject,
        text: lines.join("\n"),
        html: emailShell(
          "New access request",
          '<div style="margin-top:14px;">' + notificationBody + "</div>",
          "Sent by the fraeno.com contact form."
        ),
      });
    } catch (error) {
      console.log(`contact send failed: ${error}`);
      return reject(502, "the message could not be delivered; email thabhelo@deepubuntu.com directly");
    }

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
        html: emailShell("We got your request", confirmationBody, 
          "You are receiving this one-time message because you requested access at fraeno.com."
        ),
      });
    } catch (error) {
      console.log(`confirmation send failed: ${error}`);
    }

    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  },
};
