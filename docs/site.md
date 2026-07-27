# Fraeno website

The static product site lives in `site/`. It contains no customer code, runtime
secrets, analytics scripts, or dependency on the GitHub App control plane.

## Deploy

The production Cloudflare Pages project is named `fraeno`.

```bash
wrangler pages deploy site \
  --project-name fraeno \
  --branch main \
  --commit-hash "$(git rev-parse HEAD)"
```

Cloudflare Pages serves the deployed site at its generated `pages.dev` address.
The production custom domains are:

- `fraeno.com`
- `www.fraeno.com`

Both domains must be added through the Pages custom-domain workflow before
their DNS records are created. Do not point a manual CNAME at Pages without
first associating the hostname with the Pages project.
