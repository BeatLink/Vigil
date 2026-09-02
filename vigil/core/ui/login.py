"""The sign-in page and the routes behind it.

Deliberately plain HTML rather than a NiceGUI page: a session cookie has to be
set on an HTTP response, which a websocket-driven event cannot do, and the one
page that must work before anything else is authenticated should not depend on
the dashboard's socket coming up. It is styled from the same Halon token sheet
the dashboard uses (``static/halon-tokens.css``, inlined here), so it follows
the browser's light/dark scheme and states no color of its own.
"""

import html
import logging
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Deque, Dict, Optional

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from vigil.core.ui import theme
from vigil.core.ui.auth import (
    COOKIE_NAME, CSRF_COOKIE_NAME, LOGIN_PATH, LOGOUT_PATH, AuthConfig,
    check_credentials, clear_session_cookie, decode_next, is_secure,
    issue_session, read_session, set_session_cookie,
)

_TOKENS_CSS = (Path(__file__).parent / 'static' / 'halon-tokens.css').read_text()

# Failed sign-ins tolerated from one address before it is asked to wait.
MAX_ATTEMPTS = 5
ATTEMPT_WINDOW_SECONDS = 300


class LoginThrottle:
    """Per-address failure counter that slows password guessing to a crawl."""

    def __init__(self, max_attempts: int = MAX_ATTEMPTS,
                 window_seconds: int = ATTEMPT_WINDOW_SECONDS):
        self._max = max_attempts
        self._window = window_seconds
        self._failures: Dict[str, Deque[float]] = defaultdict(deque)

    def _recent(self, address: str) -> Deque[float]:
        failures = self._failures[address]
        cutoff = time.monotonic() - self._window
        while failures and failures[0] < cutoff:
            failures.popleft()
        return failures

    def retry_after(self, address: str) -> int:
        """Seconds this address must wait, or 0 if it may try now."""
        failures = self._recent(address)
        if len(failures) < self._max:
            return 0
        return max(1, int(failures[0] + self._window - time.monotonic()))

    def record_failure(self, address: str) -> None:
        """Count one wrong password against this address."""
        self._recent(address).append(time.monotonic())

    def clear(self, address: str) -> None:
        """Forget an address's failures once it signs in successfully."""
        self._failures.pop(address, None)


def _client_address(request: Request) -> str:
    """The address to throttle, taking the proxy's first hop when there is one."""
    forwarded = request.headers.get('x-forwarded-for', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.client.host if request.client else 'unknown'


_PAGE_CSS = """
* { box-sizing: border-box; }
body {
    margin: 0;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: var(--space-7);
    background: var(--surface-root);
    color: var(--text-body);
    font-family: var(--font-family-interface);
    font-size: var(--text-prose);
    line-height: var(--line-height-body);
}
.card {
    width: 100%;
    max-width: 360px;
    padding: var(--space-8);
    background: var(--surface-default);
    border: var(--border-width) solid var(--border-default);
    border-radius: var(--radius-window);
    box-shadow: var(--shadow-card);
}
.brand { display: flex; align-items: center; gap: var(--space-4); }
.brand h1 {
    margin: 0;
    font-size: var(--text-h2);
    font-weight: 600;
    letter-spacing: 0.02em;
    color: var(--text-heading);
}
.lede {
    margin: var(--space-4) 0 var(--space-7);
    font-size: var(--text-caption);
    color: var(--text-secondary);
}
.error {
    margin: 0 0 var(--space-6);
    padding: var(--space-4) var(--space-5);
    border: var(--border-width) solid var(--status-danger);
    border-radius: var(--radius-default);
    font-size: var(--text-control);
    color: var(--status-danger);
}
form { display: flex; flex-direction: column; }
label {
    margin-bottom: var(--space-2);
    font-size: var(--text-label);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-tertiary);
}
input[type="text"], input[type="password"] {
    height: var(--frame-height);
    margin-bottom: var(--space-6);
    padding: 0 var(--control-padding-x);
    background: var(--surface-default);
    color: var(--text-body);
    border: var(--border-width) solid var(--border-control);
    border-radius: var(--radius-default);
    font: inherit;
    font-size: var(--text-control);
}
input[type="text"]:focus, input[type="password"]:focus {
    outline: none;
    border-color: var(--border-focus);
    box-shadow: 0 0 0 var(--focus-ring-width) var(--focus-ring);
}
.remember {
    display: flex;
    align-items: center;
    gap: var(--space-4);
    margin-bottom: var(--space-7);
    font-size: var(--text-control);
    font-weight: 400;
    text-transform: none;
    letter-spacing: normal;
    color: var(--text-secondary);
}
.remember input { margin: 0; accent-color: var(--accent); }
button {
    height: var(--control-height);
    background: var(--accent);
    color: var(--text-on-fill);
    border: none;
    border-radius: var(--radius-default);
    font: inherit;
    font-size: var(--text-control);
    font-weight: 600;
    cursor: pointer;
}
button:hover { filter: brightness(1.08); }
button:focus-visible {
    outline: none;
    box-shadow: 0 0 0 var(--focus-ring-width) var(--focus-ring);
}
"""


def _render_page(*, error: Optional[str], next_token: str, csrf: str,
                 username: str) -> str:
    """The login document, with the Halon token sheet inlined for both schemes."""
    error_block = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ''
    # A pinned theme.scheme must win here too, or signing in flashes the other
    # scheme on the way to a dashboard that never uses it.
    scheme = theme.forced_scheme()
    pinned = f' data-theme="{scheme}"' if scheme in ('light', 'dark') else ''
    return f"""<!DOCTYPE html>
<html lang="en"{pinned}>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in — Vigil</title>
<link rel="icon" href="/icon.svg">
<style>
{_TOKENS_CSS}
{theme.override_css()}
{_PAGE_CSS}
</style>
</head>
<body>
<main class="card">
  <div class="brand">
    <img src="/icon.svg" alt="" width="28" height="28">
    <h1>Vigil</h1>
  </div>
  <p class="lede">Sign in to reach the dashboard.</p>
  {error_block}
  <form method="post" action="{LOGIN_PATH}">
    <input type="hidden" name="next" value="{html.escape(next_token, quote=True)}">
    <input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">
    <label for="username">Username</label>
    <input id="username" name="username" type="text" autocomplete="username"
           value="{html.escape(username, quote=True)}" required autofocus>
    <label for="password">Password</label>
    <input id="password" name="password" type="password"
           autocomplete="current-password" required>
    <label class="remember">
      <input type="checkbox" name="remember" value="1">
      <span>Keep me signed in</span>
    </label>
    <button type="submit">Sign in</button>
  </form>
</main>
</body>
</html>
"""


def _page_response(*, secure: bool, error: Optional[str] = None, next_token: str = '',
                   username: str = '', status_code: int = 200) -> Response:
    """Render the login page and mint the CSRF token this copy of it submits."""
    csrf = secrets.token_urlsafe(24)
    response = HTMLResponse(
        _render_page(error=error, next_token=next_token, csrf=csrf, username=username),
        status_code=status_code,
    )
    response.set_cookie(
        CSRF_COOKIE_NAME, csrf, path=LOGIN_PATH,
        httponly=True, samesite='lax', secure=secure,
    )
    # A cached copy would submit a CSRF token the browser no longer holds.
    response.headers['Cache-Control'] = 'no-store'
    return response


def register_login_routes(app: Any, config: AuthConfig) -> None:
    """Mount ``/login`` and ``/logout``; the session gate lets both through."""
    throttle = LoginThrottle()

    @app.get(LOGIN_PATH)
    async def login_form(request: Request):
        next_token = request.query_params.get('next', '')
        if read_session(request.cookies.get(COOKIE_NAME), config):
            return RedirectResponse(decode_next(next_token), status_code=303)
        return _page_response(next_token=next_token, secure=is_secure(request))

    @app.post(LOGIN_PATH)
    async def login_submit(request: Request):
        form = await request.form()
        next_token = str(form.get('next') or '')
        secure = is_secure(request)
        address = _client_address(request)

        wait = throttle.retry_after(address)
        if wait:
            logging.warning(f"auth: throttling sign-in attempts from {address}")
            response = _page_response(
                error=f"Too many failed attempts. Try again in {wait} seconds.",
                next_token=next_token, status_code=429, secure=secure,
            )
            response.headers['Retry-After'] = str(wait)
            return response

        submitted = str(form.get('csrf') or '')
        expected = request.cookies.get(CSRF_COOKIE_NAME) or ''
        if not expected or not secrets.compare_digest(submitted, expected):
            return _page_response(
                error='Your sign-in form expired. Please try again.',
                next_token=next_token, status_code=400, secure=secure,
            )

        username = str(form.get('username') or '')
        password = str(form.get('password') or '')
        if not check_credentials(config, username, password):
            throttle.record_failure(address)
            logging.warning(f"auth: failed sign-in for {username!r} from {address}")
            return _page_response(
                error='Incorrect username or password.', next_token=next_token,
                username=username, status_code=401, secure=secure,
            )

        throttle.clear(address)
        remember = bool(form.get('remember'))
        token, lifetime = issue_session(config, remember)
        response = RedirectResponse(decode_next(next_token), status_code=303)
        # Without "keep me signed in" the cookie is left session-scoped, so
        # closing the browser ends it well before the token itself expires.
        set_session_cookie(response, token, lifetime if remember else None, secure)
        response.delete_cookie(CSRF_COOKIE_NAME, path=LOGIN_PATH)
        logging.info(f"auth: {username!r} signed in from {address}")
        return response

    @app.get(LOGOUT_PATH)
    @app.post(LOGOUT_PATH)
    async def logout(request: Request):
        response = RedirectResponse(LOGIN_PATH, status_code=303)
        clear_session_cookie(response)
        return response
