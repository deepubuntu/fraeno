import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(
  new URL("../src/index.js", import.meta.url),
  "utf8"
);
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString(
  "base64"
)}`;
const worker = (await import(moduleUrl)).default;

function base64url(value) {
  return Buffer.from(value).toString("base64url");
}

async function adminToken(privateKey, keyId, overrides = {}) {
  const now = Math.floor(Date.now() / 1000);
  const header = base64url(JSON.stringify({ alg: "RS256", kid: keyId }));
  const payload = base64url(
    JSON.stringify({
      aud: "deepubuntu-32f9e",
      iss: "https://securetoken.google.com/deepubuntu-32f9e",
      sub: "admin-user",
      exp: now + 3600,
      iat: now,
      auth_time: now,
      isAdmin: true,
      ...overrides,
    })
  );
  const signature = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    privateKey,
    new TextEncoder().encode(`${header}.${payload}`)
  );
  return `${header}.${payload}.${Buffer.from(signature).toString("base64url")}`;
}

for (const outcome of [201, 401, 429, 500, "timeout", "missing-key"]) {
  test(`access request survives Brevo outcome ${outcome}`, async (t) => {
    const records = new Map();
    const messages = [];
    const env = {
      CONTACTS: {
        async get(key, type) {
          const value = records.get(key);
          if (value === undefined) {
            return null;
          }
          return type === "json" ? JSON.parse(value) : value;
        },
        async put(key, value) {
          records.set(key, value);
        },
      },
      BREVO_API_KEY: outcome === "missing-key" ? undefined : "test-api-key",

    };
    const errors = [];
    t.mock.method(console, "error", (message) => errors.push(message));
    t.mock.method(globalThis, "fetch", async (url, options) => {
      assert.equal(url, "https://api.brevo.com/v3/smtp/email");
      assert.equal(options.method, "POST");
      assert.equal(options.headers["api-key"], "test-api-key");
      assert.equal(options.redirect, "manual");
      assert.ok(options.signal instanceof AbortSignal);
      messages.push(JSON.parse(options.body));
      if (outcome === "timeout") throw new DOMException("Timed out", "TimeoutError");
      return new Response("{}", { status: outcome });
    });
    const request = new Request("https://fraeno.com/api/contact", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Origin: "https://fraeno.com",
      },
      body: JSON.stringify({
        name: "Kelvin",
        email: "kelvin@example.com",
        company: "NVIDIA",
        message: "Medical robotics",
        updates: true,
        website: "",
        dwell_ms: 4000,
      }),
    });

    const response = await worker.fetch(request, env);

    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { ok: true });
    assert.equal(messages.length, outcome === "missing-key" ? 0 : 1);
    assert.equal(errors.length, outcome === 201 ? 0 : 1);
    if (messages.length) {
      const mail = messages[0];
      assert.deepEqual(mail.to, [{ email: "kelvin@example.com" }]);
      assert.deepEqual(mail.sender, { name: "Fraeno", email: "contact@fraeno.com" });
      assert.equal(mail.replyTo.email, "thabhelo@deepubuntu.com");
      assert.equal(mail.subject, "We received your Fraeno access request");
      assert.match(mail.htmlContent, /We got your request/);
      assert.doesNotMatch(mail.htmlContent, /New access request/);
      assert.match(mail.textContent, /Thanks for reaching out/);
      assert.match(mail.headers["List-Unsubscribe"], /https:\/\/fraeno.com\/api\/unsubscribe/);
      assert.equal(mail.headers["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click");
    }

    const stored = JSON.parse(records.get("kelvin@example.com"));
    assert.equal(stored.name, "Kelvin");
    assert.equal(stored.submissions, 1);
    assert.equal(stored.updates, true);
  });

}

test("physics waitlist stores interest separately without sending email or erasing an access request", async () => {
  const records = new Map();
  const env = {
    CONTACTS: {
      async get(key, type) {
        const value = records.get(key);
        return value && type === "json" ? JSON.parse(value) : value || null;
      },
      async put(key, value) { records.set(key, value); },
    },
  };
  let emailCalls = 0;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => { emailCalls++; throw new Error("unexpected email"); };
  const submit = (email, dwell_ms = 4000) => worker.fetch(
    new Request("https://fraeno.com/api/waitlist", {
      method: "POST",
      headers: { Origin: "https://fraeno.com", "Content-Type": "application/json" },
      body: JSON.stringify({ email, website: "", dwell_ms }),
    }),
    env
  );
  try {
    assert.equal((await submit("bad-address")).status, 400);
    assert.equal((await submit("pilot@example.com", 10)).status, 400);
    assert.equal((await submit("Pilot@Example.com")).status, 200);
    const first = JSON.parse(records.get("pilot@example.com"));
    assert.equal(first.physics_waitlist, true);
    assert.equal(first.updates, false);
    assert.equal(first.submissions, 0);
    records.set("pilot@example.com", JSON.stringify({ ...first, name: "Pilot", github: "pilot", submissions: 1, last_message: "robot" }));
    assert.equal((await submit("pilot@example.com")).status, 200);
    const second = JSON.parse(records.get("pilot@example.com"));
    assert.equal(second.name, "Pilot");
    assert.equal(second.github, "pilot");
    assert.equal(second.last_message, "robot");
    assert.equal(second.physics_waitlisted_at, first.physics_waitlisted_at);
    assert.equal(emailCalls, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("admin configuration exposes only the public Firebase client settings", async () => {
  const response = await worker.fetch(
    new Request("https://fraeno.com/api/admin/config"),
    {
      FIREBASE_API_KEY: "public-browser-key",
      FIREBASE_PROJECT_ID: "deepubuntu-32f9e",
      FIREBASE_AUTH_DOMAIN: "deepubuntu-32f9e.firebaseapp.com",
    }
  );

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    apiKey: "public-browser-key",
    authDomain: "deepubuntu-32f9e.firebaseapp.com",
    projectId: "deepubuntu-32f9e",
  });
  assert.equal(response.headers.get("Cache-Control"), "no-store");
});

test("only a valid Firebase admin token can read access requests", async () => {
  const keys = await crypto.subtle.generateKey(
    {
      name: "RSASSA-PKCS1-v1_5",
      modulusLength: 2048,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["sign", "verify"]
  );
  const keyId = "test-key";
  const publicJwk = await crypto.subtle.exportKey("jwk", keys.publicKey);
  publicJwk.kid = keyId;
  publicJwk.alg = "RS256";
  publicJwk.use = "sig";
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ keys: [publicJwk] }), {
      headers: { "Content-Type": "application/json" },
    });
  const records = new Map([
    [
      "kelvin@example.com",
      JSON.stringify({
        name: "Kelvin",
        email: "kelvin@example.com",
        company: "NVIDIA",
        last_seen: "2026-08-17T07:40:00.000Z",
        unsubscribe_token: "private-token",
      }),
    ],
  ]);
  const env = {
    FIREBASE_PROJECT_ID: "deepubuntu-32f9e",
    CONTACTS: {
      async list() {
        return {
          keys: [...records.keys()].map((name) => ({ name })),
          list_complete: true,
        };
      },
      async get(key, type) {
        const value = records.get(key);
        return type === "json" && value ? JSON.parse(value) : value || null;
      },
    },
  };

  try {
    const missing = await worker.fetch(
      new Request("https://fraeno.com/api/admin/leads"),
      env
    );
    assert.equal(missing.status, 401);

    const nonAdmin = await adminToken(keys.privateKey, keyId, { isAdmin: false });
    const denied = await worker.fetch(
      new Request("https://fraeno.com/api/admin/leads", {
        headers: { Authorization: `Bearer ${nonAdmin}` },
      }),
      env
    );
    assert.equal(denied.status, 401);

    const token = await adminToken(keys.privateKey, keyId);
    const accepted = await worker.fetch(
      new Request("https://fraeno.com/api/admin/leads", {
        headers: { Authorization: `Bearer ${token}` },
      }),
      env
    );
    assert.equal(accepted.status, 200);
    const payload = await accepted.json();
    assert.equal(payload.leads.length, 1);
    assert.equal(payload.leads[0].email, "kelvin@example.com");
    assert.equal(payload.leads[0].unsubscribe_token, undefined);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
