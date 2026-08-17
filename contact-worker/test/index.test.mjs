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
  assert.doesNotMatch(messages[0].html, /New access request/);

  const stored = JSON.parse(records.get("kelvin@example.com"));
  assert.equal(stored.name, "Kelvin");
  assert.equal(stored.submissions, 1);
  assert.equal(stored.updates, true);
});
