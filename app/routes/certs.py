"""SSL status and certificate switching."""
import re

from fastapi import APIRouter, Depends, HTTPException

from app import auth, config, envfile, ssl_status, util

router = APIRouter()


@router.get("/api/ssl-status")
async def api_ssl_status(refresh: int = 0, username: str = Depends(auth.verify_auth)):
    status = dict(await util.run_blocking(ssl_status.get_ssl_status, bool(refresh)))
    status["ssl_expiry_notify"] = config.SSL_EXPIRY_NOTIFY == "1"
    return status


@router.post("/api/ssl/scan")
async def api_ssl_scan(username: str = Depends(auth.verify_auth)):
    if config.HTTPS_MODE not in ("nginx", "uvicorn"):
        raise HTTPException(status_code=400, detail="面板目前沒有啟用 HTTPS")
    result = await ssl_status.certctl_request("scan")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "搜尋失敗")
    return {"candidates": result.get("candidates", []), "domain": config.PANEL_DOMAIN, "mode": config.HTTPS_MODE}


@router.post("/api/ssl/apply")
async def api_ssl_apply(payload: dict, username: str = Depends(auth.verify_auth)):
    candidate = str(payload.get("candidate", ""))
    if not re.fullmatch(r"[0-9a-f]{16}", candidate):
        raise HTTPException(status_code=400, detail="Invalid candidate")
    if config.HTTPS_MODE == "uvicorn":
        # The helper restarts the panel to serve the new certificate; answer now with the request id
        # and let the browser ask /api/ssl/result once the panel is back.
        started = await ssl_status.certctl_request("apply", wait_until="running", candidate=candidate)
        return {"ok": True, "pending": True, "request_id": started["id"]}
    result = await ssl_status.certctl_request("apply", candidate=candidate)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "切換失敗")
    applied = result.get("applied") or {}
    if config.HTTPS_MODE == "nginx" and applied.get("cert"):
        # Nginx mode: the panel owns its .env; record the new paths so updates keep watching them.
        envfile.update_env_values({"SSL_CERTFILE": applied["cert"], "SSL_KEYFILE": applied["key"]})
        config.SSL_CERTFILE = applied["cert"]
    ssl_status.SSL_STATUS_CACHE["data"] = None
    return {"ok": True, "applied": applied, "ssl": await util.run_blocking(ssl_status.get_ssl_status, True)}


@router.get("/api/ssl/result")
async def api_ssl_result(id: str = "", username: str = Depends(auth.verify_auth)):
    if not re.fullmatch(r"[0-9a-f]{16}", id):
        raise HTTPException(status_code=400, detail="Invalid request id")
    result = ssl_status.read_certctl_result(id)
    if result.get("state") == "done":
        ssl_status.SSL_STATUS_CACHE["data"] = None
    return result


@router.post("/api/ssl-settings")
async def api_ssl_settings(payload: dict, username: str = Depends(auth.verify_auth)):
    config.SSL_EXPIRY_NOTIFY = "1" if payload.get("ssl_expiry_notify") else "0"
    envfile.update_env_values({"SSL_EXPIRY_NOTIFY": config.SSL_EXPIRY_NOTIFY})
    return {"ok": True, "ssl_expiry_notify": config.SSL_EXPIRY_NOTIFY == "1"}
