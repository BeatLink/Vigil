"""
HTTP Basic Auth for Vigil's dashboard and REST API.

Vigil ships with no authentication by default (see README roadmap) — anyone
who can reach the port gets full read access plus the ability to trigger
control actions (restart services, kill processes, etc.). This module adds an
opt-in shared-credential gate: when `auth.username` and `auth.password` (or
`auth.password_file`) are set in config.yaml, every request must present
matching HTTP Basic credentials.

Registered as ASGI middleware on the NiceGUI/FastAPI app so it covers the
dashboard pages, the REST API, and the Prometheus /metrics endpoint uniformly
— no per-route wiring to keep in sync as routes are added.
"""
import hmac
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


def _read_password(auth_settings: Dict[str, Any]) -> Optional[str]:
    if 'password' in auth_settings:
        return str(auth_settings['password'])
    password_file = auth_settings.get('password_file')
    if password_file:
        try:
            return Path(password_file).read_text(encoding='utf-8').strip()
        except OSError as e:
            logging.error(f"auth: could not read password_file {password_file}: {e}")
            return None
    return None


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, username: str, password: str):
        super().__init__(app)
        self._username = username
        self._password = password

    async def dispatch(self, request: Request, call_next):
        credentials = request.headers.get('authorization')
        if credentials and self._is_valid(credentials):
            return await call_next(request)

        return Response(
            status_code=401,
            headers={'WWW-Authenticate': 'Basic realm="Vigil"'},
        )

    def _is_valid(self, header_value: str) -> bool:
        import base64
        scheme, _, encoded = header_value.partition(' ')
        if scheme.lower() != 'basic' or not encoded:
            return False
        try:
            decoded = base64.b64decode(encoded).decode('utf-8')
        except (ValueError, UnicodeDecodeError):
            return False
        username, _, password = decoded.partition(':')
        # Constant-time comparison avoids leaking match length via timing.
        return (
            hmac.compare_digest(username, self._username)
            and hmac.compare_digest(password, self._password)
        )


def register_auth(app: Any, auth_settings: Dict[str, Any]) -> None:
    """Attach Basic Auth middleware if `auth.username`/`password` are configured."""
    username = auth_settings.get('username')
    password = _read_password(auth_settings)

    if not username and not password:
        return

    if not username or not password:
        logging.error(
            "auth: both 'username' and 'password'/'password_file' must be set — "
            "auth NOT enabled, dashboard and API are unauthenticated."
        )
        return

    app.add_middleware(BasicAuthMiddleware, username=username, password=password)
    logging.info("auth: HTTP Basic Auth enabled for dashboard and API")
