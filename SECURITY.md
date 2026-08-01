# Security

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue.
GitHub's
[**Report a vulnerability**](https://github.com/bespo-ai/script.it-cli/security/advisories/new)
form opens an advisory that only you and the maintainers can see.

Include what you found, how to reproduce it, and what an attacker could do with
it. We'll acknowledge within three business days and keep you posted until it's
resolved.

Please don't include real credentials or tokens in a report — a description of
where they leak is enough.

## What this client handles

Worth knowing when assessing a finding:

- **Credentials live in `$XDG_CONFIG_HOME/scriptit/credentials.json`**
  (default `~/.config/scriptit/credentials.json`), written mode `0600`. The
  file holds a refresh token per profile — never a password. The CLI never
  reads or transmits your password; sign-in happens in your browser.
- **Login uses a loopback handoff.** `scriptit auth login` listens on
  `127.0.0.1` on a random port and accepts a token from the signed-in page,
  matched against a nonce it generated. Self-hosted Keycloak deployments use
  the RFC 8628 device grant with PKCE (S256) instead.
- **Tokens are short-lived.** ID tokens are refreshed lazily and re-persisted;
  `scriptit auth logout` removes the stored credential.
- **Commands run remotely, not locally.** This client dispatches shell strings
  to your Script.it environment. It executes nothing on your machine, so a
  malicious response cannot run local code — but treat output as untrusted
  data, as you would from any remote host.
- **`--json` is the safe parsing surface.** Human output is formatting, not an
  API; don't screen-scrape it in scripts that make security decisions.

## Scope

Issues in this client — the credential store, the login flows, the transport —
belong here. Vulnerabilities in the Script.it platform itself, its isolation
model, or the web app can be reported the same way and reach the same team.
