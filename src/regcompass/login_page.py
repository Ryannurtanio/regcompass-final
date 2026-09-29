"""The sign-in page a hosted copy shows at /login.

One self-contained page: its styles, its fonts and its script are inline and it
asks for nothing else, so it loads before the browser has a session and on a
machine with no internet. The colours are the interface's own tokens
(ui/src/styles.css) in light and dark, and a theme chosen with the app's switch
is honoured here too. The fonts are the interface's bundled IBM Plex Sans and
Newsreader, read from the built bundle and written into the page as data, so
the page still needs no second request; without a built bundle it falls back to
the system fonts.

The script posts the pair to /api/login and, on success, goes back to where
the browser was headed (the ``next`` parameter, checked again here the same
way the server checks it) or to the root.
"""

import base64
from pathlib import Path

_ASSETS = Path(__file__).resolve().parent / "ui_dist" / "assets"

# (family, weight, the bundled file's name before its content hash)
_FONTS = (
    ("IBM Plex Sans", 400, "ibm-plex-sans-latin-400-normal-"),
    ("IBM Plex Sans", 500, "ibm-plex-sans-latin-500-normal-"),
    ("IBM Plex Sans", 600, "ibm-plex-sans-latin-600-normal-"),
    ("Newsreader", 500, "newsreader-latin-500-normal-"),
)


def _font_faces(assets: Path = _ASSETS) -> str:
    """@font-face rules for the fonts the page uses, each file inlined."""
    rules = []
    for family, weight, prefix in _FONTS:
        found = sorted(assets.glob(f"{prefix}*.woff2")) if assets.is_dir() else []
        if not found:
            continue
        data = base64.b64encode(found[0].read_bytes()).decode("ascii")
        rules.append(
            f"@font-face {{ font-family: '{family}'; font-style: normal;"
            f" font-weight: {weight}; font-display: swap;"
            f" src: url(data:font/woff2;base64,{data}) format('woff2'); }}"
        )
    return "\n".join(rules)


_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="robots" content="noindex">
<title>Sign in to RegCompass</title>
<script>
try {
  var t = localStorage.getItem('regcompass.theme');
  if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
} catch (e) {}
</script>
<style>
/*FONTS*/
:root {
  color-scheme: light;
  --paper: #edf0ec;
  --surface: #f9faf8;
  --ink: #14252e;
  --muted: #55646b;
  --rule: #d2d9d5;
  --route: #2b4fb8;
  --fail: #a8362a;
  --fail-soft: #f6deda;
  --on-solid: #ffffff;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme='light']) {
    color-scheme: dark;
    --paper: #0d161b;
    --surface: #142027;
    --ink: #e3e9e6;
    --muted: #98a7ad;
    --rule: #27363e;
    --route: #93acff;
    --fail: #f2887a;
    --fail-soft: #40201c;
    --on-solid: #0d161b;
  }
}
:root[data-theme='dark'] {
  color-scheme: dark;
  --paper: #0d161b;
  --surface: #142027;
  --ink: #e3e9e6;
  --muted: #98a7ad;
  --rule: #27363e;
  --route: #93acff;
  --fail: #f2887a;
  --fail-soft: #40201c;
  --on-solid: #0d161b;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  background: var(--paper);
  color: var(--ink);
  font-family: 'IBM Plex Sans', system-ui, -apple-system, 'Segoe UI', sans-serif;
  font-size: 15px;
  line-height: 1.45;
  -webkit-font-smoothing: antialiased;
}
.page {
  min-height: 100%;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
}
.intro {
  padding: 72px 80px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 48px;
  border-right: 1px solid var(--rule);
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  font-weight: 600;
  font-size: 18px;
}
.pitch { display: flex; flex-direction: column; gap: 18px; max-width: 520px; }
.pitch h1 {
  font-family: 'Newsreader', Georgia, serif;
  font-weight: 500;
  font-size: 46px;
  line-height: 1.1;
  letter-spacing: -0.01em;
}
.pitch p { color: var(--muted); font-size: 16px; max-width: 46ch; }
.k { color: var(--muted); font-size: 13px; }
.signin {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px 24px;
  background: var(--surface);
}
form { width: 100%; max-width: 380px; display: flex; flex-direction: column; gap: 18px; }
h2 { font-size: 26px; font-weight: 500; }
.field { display: flex; flex-direction: column; gap: 6px; }
label { font-weight: 500; }
input {
  font: inherit;
  height: 44px;
  padding: 0 12px;
  border-radius: 8px;
  border: 1px solid var(--rule);
  background: var(--surface);
  color: var(--ink);
  width: 100%;
}
input:focus-visible, button:focus-visible {
  outline: 2px solid var(--route);
  outline-offset: 2px;
}
button {
  appearance: none;
  font: inherit;
  font-weight: 600;
  height: 46px;
  border-radius: 8px;
  border: 1px solid var(--route);
  background: var(--route);
  color: var(--on-solid);
  cursor: pointer;
}
button:hover:not(:disabled) { background: var(--ink); border-color: var(--ink); color: var(--surface); }
button:disabled { opacity: 0.55; cursor: default; }
.error:empty { display: none; }
.error {
  padding: 12px 14px;
  border-radius: 8px;
  background: var(--fail-soft);
  color: var(--fail);
  font-size: 14px;
}
@media (max-width: 900px) {
  .page { grid-template-columns: minmax(0, 1fr); }
  .intro { padding: 24px 16px 8px; border-right: 0; gap: 20px; }
  .pitch { gap: 10px; }
  .pitch h1 { font-size: 30px; }
  .pitch p { font-size: 15px; }
  .intro .k { display: none; }
  .signin { align-items: flex-start; padding: 24px 16px 40px; background: transparent; }
}
</style>
</head>
<body>
<div class="page">
<section class="intro">
  <div class="brand">
    <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true"><circle cx="11" cy="11" r="9.5" fill="none" stroke="currentColor" stroke-width="1.5"></circle><path d="M11 4 L13.2 11 L11 18 L8.8 11 Z" fill="currentColor" opacity=".25"></path><path d="M11 4 L13.2 11 L8.8 11 Z" fill="var(--route)"></path></svg>
    <span>RegCompass</span>
  </div>
  <div class="pitch">
    <h1>Every finding points to the exact words of the law.</h1>
    <p>RegCompass maps national legislation to the RDTII 2.1 Indicators. Each Mapping carries a quote proven word for word against the source, with its page.</p>
  </div>
  <div class="k">RDTII 2.1, verified provision review</div>
</section>
<main class="signin">
  <form id="login-form" novalidate>
    <h2>Sign in</h2>
    <div class="field">
      <label for="user">User name</label>
      <input id="user" name="username" type="text" autocomplete="username"
        autocapitalize="none" autocorrect="off" spellcheck="false" required autofocus>
    </div>
    <div class="field">
      <label for="password">Password</label>
      <input id="password" name="password" type="password"
        autocomplete="current-password" required>
    </div>
    <button id="submit" type="submit">Sign in</button>
    <p class="error" id="error" role="alert" aria-live="assertive"></p>
    <p class="k">The session lasts 12 hours on this browser.</p>
  </form>
</main>
</div>
<script>
(function () {
  var form = document.getElementById('login-form');
  var user = document.getElementById('user');
  var password = document.getElementById('password');
  var button = document.getElementById('submit');
  var error = document.getElementById('error');

  // Only a path on this site: one leading slash, no backslash, no control
  // character, and never the login page itself.
  function nextPath() {
    var raw = new URLSearchParams(window.location.search).get('next') || '/';
    if (raw.charAt(0) !== '/' || raw.charAt(1) === '/' || raw.indexOf('\\') !== -1) return '/';
    if (/[\u0000-\u001f\u007f]/.test(raw)) return '/';
    if (raw.split('?')[0].replace(/\/+$/, '') === '/login') return '/';
    return raw;
  }

  function fail(message) {
    error.textContent = message;
    button.disabled = false;
    button.textContent = 'Sign in';
    password.focus();
    password.select();
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    if (!user.value || !password.value) {
      fail('Enter the user name and the password.');
      if (!user.value) user.focus();
      return;
    }
    error.textContent = '';
    button.disabled = true;
    button.textContent = 'Signing in…';
    fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ user: user.value, password: password.value })
    }).then(function (r) {
      if (r.ok) {
        window.location.replace(nextPath());
        return;
      }
      return r.json().catch(function () { return {}; }).then(function (body) {
        var detail = body && typeof body.detail === 'string' ? body.detail : '';
        fail(detail ? detail.charAt(0).toUpperCase() + detail.slice(1) + '.'
                    : 'Sign-in failed (HTTP ' + r.status + ').');
      });
    }, function () {
      fail('The server did not answer. Check the connection and try again.');
    });
  });
})();
</script>
</body>
</html>
"""

LOGIN_PAGE_HTML = _PAGE.replace("/*FONTS*/", _font_faces(), 1)
