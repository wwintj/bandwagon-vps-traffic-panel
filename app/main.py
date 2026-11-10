"""VPS Traffic Panel: application assembly.

Feature code lives in its own module (auth, traffic, bandwagon, ddns, telegram, ssl_status,
system, net, envfile, util) and the HTTP endpoints in app/routes. Runtime settings are attributes
of app.config, read and written as config.NAME so a change made from the panel is seen everywhere.
"""
import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import auth, config, ddns, telegram, traffic, util
from app.collector import collector_instance
from app.database import init_db
from app.routes import bandwagon as bandwagon_routes
from app.routes import certs as certs_routes
from app.routes import pages as pages_routes
from app.routes import settings as settings_routes
from app.routes import traffic as traffic_routes

CSRF_HEADER = "x-panel-request"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not auth.credentials_configured():
        util.logger.error("AUTH_USERNAME / AUTH_PASSWORD is empty; the panel will reject all logins until .env is fixed.")
    try:
        auth.migrate_plaintext_password()
    except Exception as exc:
        util.logger.warning("Could not migrate the plaintext password to a hash: %s", exc)
    init_db()
    tasks = [
        asyncio.create_task(collector_instance.start()),
        asyncio.create_task(telegram.telegram_daily_notifier()),
        asyncio.create_task(ddns.ddns_background_updater()),
        asyncio.create_task(traffic.traffic_alert_watcher()),
    ]
    yield
    collector_instance.running = False
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    # Browsers resend cookies (and cached Basic credentials) on cross-site form posts. A custom
    # header cannot be set by a plain form, and cross-origin fetch with it needs a CORS preflight
    # we never allow.
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
        return JSONResponse({"detail": "Missing X-Panel-Request header"}, status_code=403)
    return await call_next(request)


app.mount("/static", StaticFiles(directory=os.path.join(config.BASE_DIR, "app/static")), name="static")
for module in (pages_routes, settings_routes, traffic_routes, bandwagon_routes, certs_routes):
    app.include_router(module.router)
