# Contact Worker

Access requests are saved in Cloudflare KV. Brevo sends the customer confirmation
from `contact@fraeno.com`, with replies addressed to `thabhelo@deepubuntu.com`.

Before deploying, verify `fraeno.com` and its sender in the DeepUbuntu Brevo
account, then install that account's API key as the `BREVO_API_KEY` Worker secret.
Use a dedicated Fraeno key and never commit keys to Git. The DeepUbuntu Labs
Brevo account also serves Speculum Mundi, so both share its sending allowance.

Brevo's account allowance is shared with its other email sends. A provider failure
is logged without discarding the saved enquiry. Ambiguous failures are not retried
automatically because the confirmation may already have been accepted.

Run `node --test contact-worker/test/index.test.mjs` from the repository root.
Deployment uses `contact-worker/wrangler.jsonc`. Removing the email binding does
not remove Cloudflare hosting, KV or DNS, and does not cancel Workers Paid.
