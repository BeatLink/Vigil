"""The answer every HTTP request gets while the engine is still loading its saved state, in place of a proxy's Bad Gateway."""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse

# Seconds a browser or client waits before asking again.
RETRY_AFTER = 5

_PASS_THROUGH = ('/_nicegui', '/icon.svg')
_MACHINE_PATHS = ('/api/', '/metrics')

STARTING_PAGE = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{RETRY_AFTER}">
<title>Vigil starting</title>
<link rel="icon" href="/icon.svg">
<style>
  :root {{ color-scheme: light dark; --bg: #f6f7f9; --fg: #1d2330; --muted: #5d6677; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg: #12151b; --fg: #e6e9ef; --muted: #9aa3b2; }} }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 16px;
         background: var(--bg); color: var(--fg); font: 16px/1.5 system-ui, sans-serif; }}
  main {{ max-width: 28rem; text-align: center; }}
  h1 {{ font-size: 1.25rem; margin: 0 0 .5rem; }}
  p {{ margin: 0; color: var(--muted); }}
</style>
</head>
<body>
<main>
<h1>Vigil is starting</h1>
<p>Loading saved history. This page reloads by itself.</p>
</main>
</body>
</html>
"""


class StartingUpMiddleware(BaseHTTPMiddleware):
    """Answers 503 until `engine.ready`: a page that reloads itself for browsers, JSON for the API and exporters."""

    def __init__(self, app, engine):
        super().__init__(app)
        self.engine = engine

    async def dispatch(self, request, call_next):
        path = request.url.path
        if self.engine.ready or path.startswith(_PASS_THROUGH):
            return await call_next(request)
        headers = {'Retry-After': str(RETRY_AFTER)}
        if path.startswith(_MACHINE_PATHS):
            return JSONResponse({'status': 'starting'}, status_code=503, headers=headers)
        return HTMLResponse(STARTING_PAGE, status_code=503, headers=headers)
