"""HTML pages and login / logout."""
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import __version__, auth, config, sessions, util
from app.collector import collector_instance

router = APIRouter()


templates = Jinja2Templates(directory=os.path.join(config.BASE_DIR, "app/templates"))


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if auth.has_valid_session(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"panel_title": config.PANEL_TITLE, "version": __version__})


@router.post("/api/login")
async def api_login(payload: dict, request: Request):
    client_ip = auth.get_client_ip(request)
    if auth.is_auth_locked(client_ip):
        raise HTTPException(status_code=429, detail="嘗試次數過多，請 15 分鐘後再試")
    if not auth.credentials_configured():
        raise HTTPException(status_code=503, detail="面板尚未設定帳號密碼，請檢查 .env")
    username = str(payload.get("username", ""))
    password = str(payload.get("password", ""))
    if not await util.run_blocking(auth.check_credentials, username, password):
        auth.record_auth_failure(client_ip, username, password)
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤")
    auth.AUTH_FAILURES.pop(client_ip, None)
    remember = bool(payload.get("remember"))
    ttl = auth.SESSION_TTL_REMEMBER if remember else auth.SESSION_TTL
    token = await util.run_blocking(sessions.create, ttl)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        auth.SESSION_COOKIE, token, max_age=ttl if remember else None, path="/",
        httponly=True, samesite="strict", secure=auth.request_is_https(request),
    )
    return response


@router.post("/api/logout")
async def api_logout(request: Request):
    sessions.delete(request.cookies.get(auth.SESSION_COOKIE, ""))
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


@router.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    try:
        auth.verify_auth(request, await auth.security(request))
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=302)
        raise
    return templates.TemplateResponse(request, "index.html", {
        "interface": collector_instance.interface,
        "panel_title": config.PANEL_TITLE,
        "panel_subtitle": config.PANEL_SUBTITLE,
        "version": __version__,
    })


@router.get("/logout")
async def logout(request: Request):
    sessions.delete(request.cookies.get(auth.SESSION_COOKIE, ""))
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response
