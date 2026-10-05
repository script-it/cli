---
name: scriptit-browser-cookies
description: Copy the user's logged-in browser cookies for chosen sites into their Script.it sandbox, so sandbox browser automations (Playwright) run as the user. Use when the user asks to "send my cookies/login to Script.it" or when a sandbox scrape/download fails because it isn't logged in.
---

# Send browser logins to the Script.it sandbox

Copies cookies for sites you choose out of your local Chrome and into your
Script.it sandbox at `~/.browser/storage_state.json`. Sandbox Playwright
scripts then load that file and act as you on those sites.

Requires the `scriptit` CLI, logged in (`scriptit auth status`). Chrome on
macOS assumed; see "Other browsers" at the end otherwise.

## 1. Ask which sites, and warn

Get an explicit list of domains from the user (e.g. `example.com`). **Never
export every cookie.** Tell the user these cookies let the sandbox act as them
on those sites, from a datacenter IP — some sites may log the session out or
raise a "new sign-in" alert, especially those that bind a session to its
original IP or device.

For each site you need the cookie that actually proves login, so the export can
fail loudly when it's absent. Open the site's cookies in Chrome DevTools
(Application → Storage → Cookies) while logged in and pick the obvious
session/auth cookie, then pass it to `--require` in §2. To skip the guard, pass
`--require ""` — the verify step in §3 is then the only safety net.

## 2. Export from the active Chrome profile

`browser_cookie3` defaults to Chrome's `Default` profile, which is usually
**not** the one you use — exporting from it yields stale, logged-out cookies.
This reads the real last-used profile and drops already-expired cookies
(Playwright silently discards expired cookies, leaving you logged out with no
error). It aborts if a required login cookie is missing.

Replace the `--require` names (the login cookies found in §1) and the domains
with the user's:

```bash
uv run --quiet --with browser-cookie3 python - \
  --require SESSION_COOKIE,OTHER_SESSION_COOKIE \
  example.com other.com <<'PY'
import browser_cookie3, json, sys, time
from pathlib import Path

args = sys.argv[1:]
required = []
if args and args[0] == "--require":
    required = [c for c in args[1].split(",") if c]
    args = args[2:]
domains = args
if not domains:
    sys.exit("usage: ... [--require c1,c2] domain [domain ...]")

root = Path.home() / "Library/Application Support/Google/Chrome"
profile = json.loads((root / "Local State").read_text())["profile"].get("last_used") or "Default"
cookie_file = str(root / profile / "Cookies")

out = []
for d in domains:
    for c in browser_cookie3.chrome(cookie_file=cookie_file, domain_name=d):
        if c.expires and c.expires < time.time():
            continue  # Playwright drops expired cookies anyway
        out.append({"name": c.name, "value": c.value, "domain": c.domain,
                    "path": c.path, "expires": c.expires or -1,
                    "secure": bool(c.secure),
                    "httpOnly": c.has_nonstandard_attr("HTTPOnly"),
                    "sameSite": "Lax"})

names = {c["name"] for c in out}
missing = [r for r in required if r not in names]
if missing:
    sys.exit(f"profile {profile!r}: missing live login cookie(s) {missing}; "
             f"log in to those sites in that Chrome profile, then re-run")

json.dump({"cookies": out, "origins": []}, open("storage_state.json", "w"))
print(f"profile {profile}: {len(out)} live cookies; required present: {sorted(required)}")
PY
```

If this prints 0 cookies or your browser isn't Chrome, use the fallback in the
last section.

## 3. Upload, move into place, verify

Push the file into the session's `data_files` directory, then move it into
`~/.browser` with `scriptit exec`.

```bash
SESSION_DIR=$(scriptit exec -- pwd)
scriptit fs push storage_state.json "$SESSION_DIR/data_files/storage_state.json"
scriptit exec -- 'mkdir -p ~/.browser \
  && mv data_files/storage_state.json ~/.browser/storage_state.json \
  && chmod 600 ~/.browser/storage_state.json && echo moved'
rm -f storage_state.json   # don't leave the cookie bundle on the laptop
```

Verify the sandbox is actually logged in before telling the user it worked.
Point `URL` at a page on the target site that only renders when authenticated
(an account, dashboard, or feed page) and check the final URL didn't bounce to
a login/authwall:

```bash
scriptit exec --timeout 150 -- 'URL=https://example.com/account python3 - <<"PY"
import os
from playwright.sync_api import sync_playwright
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
with sync_playwright() as p:
    with p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"]) as b:
        ctx = b.new_context(storage_state=os.path.expanduser("~/.browser/storage_state.json"), user_agent=UA)
        page = ctx.new_page()
        page.goto(os.environ["URL"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5000)
        bad = any(x in page.url for x in ("login", "authwall", "signin", "checkpoint"))
        print("final_url:", page.url, "| logged_in:", not bad)
PY'
```

## 4. Use it in a sandbox script

```python
import os
from playwright.sync_api import sync_playwright

STATE = os.path.expanduser("~/.browser/storage_state.json")
with sync_playwright() as p:
    with p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"]) as b:
        ctx = b.new_context(storage_state=STATE if os.path.exists(STATE) else None)
        page = ctx.new_page()
        ...
```

## Notes & limits

- **One file, all sites.** Re-running §2 replaces it, so always pass the full
  domain list you want, not just the new one.
- **Treat it as sensitive.** The file holds live logins. Keep the domain list
  to what you actually need, and delete it (`scriptit exec -- rm
  ~/.browser/storage_state.json`) when you're done.
- **Expiry.** It's a snapshot; when the session cookies age out, re-run §2.
- **`scriptit` shadowed?** If `scriptit` errors with
  `ModuleNotFoundError: No module named 'scriptit'`, a different `scriptit` is
  ahead of the CLI on your PATH. Reinstall the client: `pip install -e <scriptit-cli>`.
- **Other browsers / 0 cookies.** Swap `.chrome(...)` for `browser_cookie3.firefox`/
  `.edge`/`.brave`, or capture interactively: launch a *headed* Playwright
  Chromium locally, log in by hand, then
  `context.storage_state(path="storage_state.json", indexed_db=True)` (also
  captures localStorage/IndexedDB, which some SSO/Firebase logins use instead of
  cookies). Then continue from §3.
