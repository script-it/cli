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
session/auth cookie, then pass it to `--require` in §2 as `domain=cookie`. To
skip the guard, omit `--require` — the verify step in §3 is then the only
safety net.

## 2. Export from the active Chrome profile

`browser_cookie3` defaults to Chrome's `Default` profile, which is usually
**not** the one you use — exporting from it yields stale, logged-out cookies.
This reads the real last-used profile, keeps only cookies that actually belong
to a requested domain (a naive match would also pull `notexample.com` for
`example.com`), preserves each cookie's real SameSite policy, drops expired
cookies (Playwright silently discards them, leaving you logged out with no
error), and aborts if a required login cookie is missing for its site.

Replace the `--require` pairs (`domain=login_cookie`, from §1) and the domains
with the user's:

```bash
uv run --quiet --with browser-cookie3 python - \
  --require example.com=SESSION_COOKIE,other.com=OTHER_COOKIE \
  example.com other.com <<'PY'
import browser_cookie3, json, os, sqlite3, sys, time
from pathlib import Path

# Parse: [--require dom=cookie,dom=cookie] domain [domain ...]
args = sys.argv[1:]
require = {}
if args and args[0] == "--require":
    for pair in filter(None, (p.strip() for p in args[1].split(","))):
        dom, sep, cookie = pair.partition("=")
        if not sep or not cookie.strip():
            sys.exit("--require entries must be domain=cookie_name")
        require.setdefault(dom.strip().lower(), set()).add(cookie.strip())
    args = args[2:]
domains = [d.strip().lower() for d in args] or list(require)
if not domains:
    sys.exit("usage: ... [--require dom=cookie,...] domain [domain ...]")

root = Path.home() / "Library/Application Support/Google/Chrome"
profile = json.loads((root / "Local State").read_text())["profile"].get("last_used") or "Default"
pdir = root / profile
# Newer Chrome keeps the DB under Network/; older profiles keep it at the top.
cookie_file = next((str(pdir / p) for p in ("Network/Cookies", "Cookies")
                    if (pdir / p).exists()), None)
if not cookie_file:
    sys.exit(f"no cookie database for profile {profile!r} "
             f"(looked in Network/Cookies and Cookies)")

# browser_cookie3 doesn't expose SameSite; read it straight from Chrome's DB.
# Chrome stores 0=None, 1=Lax, 2=Strict, -1=unspecified.
samesite_by_key = {}
con = sqlite3.connect(f"file:{cookie_file}?mode=ro&immutable=1", uri=True)
try:
    for host, name, ss in con.execute("select host_key, name, samesite from cookies"):
        samesite_by_key[(host, name)] = {0: "None", 1: "Lax", 2: "Strict"}.get(ss, "Lax")
finally:
    con.close()

def applies(cookie_domain, site):
    """True if a browser would send this cookie to `site` — the cookie's own
    host, or a parent domain of it. Excludes unrelated substring matches."""
    d = cookie_domain.lstrip(".").lower()
    return site == d or site.endswith("." + d)

seen, out = set(), []
for c in browser_cookie3.chrome(cookie_file=cookie_file):  # read all, then filter precisely
    if not any(applies(c.domain, s) for s in domains):
        continue
    if c.expires and c.expires < time.time():
        continue
    key = (c.name, c.domain, c.path)
    if key in seen:
        continue
    seen.add(key)
    samesite = samesite_by_key.get((c.domain, c.name), "Lax")
    out.append({"name": c.name, "value": c.value, "domain": c.domain,
                "path": c.path, "expires": c.expires or -1,
                "secure": bool(c.secure) or samesite == "None",  # SameSite=None needs Secure
                "httpOnly": c.has_nonstandard_attr("HTTPOnly"),
                "sameSite": samesite})

# Guard per site: each required cookie must be live for its own domain.
missing = []
for site, names in require.items():
    have = {c["name"] for c in out if applies(c["domain"], site)}
    missing += [f"{site}:{n}" for n in sorted(names) if n not in have]
if missing:
    sys.exit(f"profile {profile!r}: missing live login cookie(s) {missing}; "
             f"log in to those sites in that Chrome profile, then re-run")

# 0600 from creation so the local copy is never world-readable.
fd = os.open("storage_state.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump({"cookies": out, "origins": []}, f)
print(f"profile {profile}: {len(out)} cookies for {domains}; required present")
PY
```

If this prints 0 cookies or your browser isn't Chrome, use the fallback in the
last section.

## 3. Upload, move into place, verify

Push the file into the session's `data_files` directory, then move it into
`~/.browser` with `scriptit exec`. Delete the local copy only once the move
succeeds, and clean up the `data_files` copy if it doesn't — otherwise the
cookies are left in a less-protected spot.

```bash
SESSION_DIR=$(scriptit exec -- pwd)
scriptit fs push storage_state.json "$SESSION_DIR/data_files/storage_state.json"
if scriptit exec -- 'mkdir -p ~/.browser \
     && mv data_files/storage_state.json ~/.browser/storage_state.json \
     && chmod 600 ~/.browser/storage_state.json && echo moved'; then
  rm -f storage_state.json
else
  echo "move failed — removing the uploaded copy from data_files"
  scriptit exec -- 'rm -f data_files/storage_state.json'
  rm -f storage_state.json
  exit 1
fi
```

Verify the sandbox is actually logged in before telling the user it worked.
Check **each** requested site: point `URL` at a page on it that only renders
when authenticated (an account, dashboard, or feed page) and confirm the final
URL didn't bounce to a login/authwall:

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
- **Treat it as sensitive.** The file holds live logins. It's written `0600`
  locally and remotely, but still: keep the domain list to what you actually
  need, and delete it (`scriptit exec -- rm ~/.browser/storage_state.json`)
  when you're done.
- **Expiry.** It's a snapshot; when the session cookies age out, re-run §2.
- **Cross-site / SSO logins.** The export preserves each cookie's SameSite, so
  `SameSite=None` cookies (third-party identity providers, embedded iframes)
  keep working. A login that lives in `localStorage`/IndexedDB rather than
  cookies (some Firebase/SSO flows) won't be captured by §2 — use the
  interactive fallback below, which saves that storage too.
- **`scriptit` shadowed?** If `scriptit` errors with
  `ModuleNotFoundError: No module named 'scriptit'`, a different `scriptit` is
  ahead of the CLI on your PATH. Reinstall the client: `pip install -e <scriptit-cli>`.
- **Other browsers / 0 cookies.** Swap `.chrome(...)` for `browser_cookie3.firefox`/
  `.edge`/`.brave`, or capture interactively: launch a *headed* Playwright
  Chromium locally, **log in only to the target sites** (the next step saves
  cookies and origin storage for *every* site the browser visited, so a stray
  tab leaks that login into the upload), then
  `context.storage_state(path="storage_state.json", indexed_db=True)` (also
  captures localStorage/IndexedDB). If in doubt, open `storage_state.json` and
  delete `cookies`/`origins` entries for domains you didn't intend to send.
  Then `chmod 600 storage_state.json` and continue from §3.
