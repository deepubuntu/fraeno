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
      aud: "fraeno-prod",
      iss: "https://securetoken.google.com/fraeno-prod",
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

test("an access request sends only the customer confirmation", async () => {
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
    EMAIL: {
      async send(message) {
        messages.push(message);
      },
    },
  };
  const request = new Request("https://fraeno.com/api/contact", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: "https://fraeno.com",
    },
    body: JSON.stringify({
      name: "Kelvin",
      email: "kelvin@example.com",
      github: "kelvin-robotics",
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
  assert.equal(messages.length, 1);
  assert.equal(messages[0].to, "kelvin@example.com");
  assert.equal(messages[0].from.name, "Fraeno");
  assert.equal(messages[0].replyTo.email, "thabhelo@deepubuntu.com");
  assert.equal(messages[0].subject, "We received your Fraeno access request");
  assert.match(messages[0].html, /We got your request/);
  assert.match(messages[0].html, /@kelvin-robotics/);
  assert.doesNotMatch(messages[0].html, /New access request/);

  const stored = JSON.parse(records.get("kelvin@example.com"));
  assert.equal(stored.name, "Kelvin");
  assert.equal(stored.github, "kelvin-robotics");
  assert.equal(stored.submissions, 1);
  assert.equal(stored.updates, true);
});

test("admin configuration exposes only the public Firebase client settings", async () => {
  const response = await worker.fetch(
    new Request("https://fraeno.com/api/admin/config"),
    {
      FIREBASE_API_KEY: "public-browser-key",
      FIREBASE_PROJECT_ID: "fraeno-prod",
      FIREBASE_AUTH_DOMAIN: "fraeno-prod.firebaseapp.com",
    }
  );

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    apiKey: "public-browser-key",
    authDomain: "fraeno-prod.firebaseapp.com",
    projectId: "fraeno-prod",
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
        github: "kelvin-robotics",
        company: "NVIDIA",
        last_seen: "2026-08-17T07:40:00.000Z",
        unsubscribe_token: "private-token",
      }),
    ],
  ]);
  const env = {
    FIREBASE_PROJECT_ID: "fraeno-prod",
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
    assert.equal(payload.leads[0].github, "kelvin-robotics");
    assert.equal(payload.leads[0].unsubscribe_token, undefined);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
