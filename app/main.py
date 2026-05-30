from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from app.routers import auth, analyze, credits, webhook
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("applyin")

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="Applyin API", version="1.0.0", docs_url="/docs", redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS — manual middleware (FastAPI CORSMiddleware conflicts with allow_origins=* + credentials) ──
class CORSMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin", "")

        # Handle preflight
        if request.method == "OPTIONS":
            response = JSONResponse(content={}, status_code=200)
            response.headers["Access-Control-Allow-Origin"]  = origin or "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Max-Age"] = "3600"
            return response

        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"]  = origin or "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        return response

app.add_middleware(CORSMiddleware)

# ── Fix double slashes ────────────────────────────────────────────────────────
class DoubleSlashMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if "//" in request.url.path:
            fixed = request.url.path.replace("//", "/")
            new_url = str(request.url).replace(request.url.path, fixed, 1)
            return RedirectResponse(url=new_url, status_code=301)
        return await call_next(request)

app.add_middleware(DoubleSlashMiddleware)

app.include_router(auth.router,     prefix="/auth",     tags=["auth"])
app.include_router(analyze.router,  prefix="/analyze",  tags=["analyze"])
app.include_router(credits.router,  prefix="/credits",  tags=["credits"])
app.include_router(webhook.router,  prefix="/webhook",  tags=["webhook"])

from app.routers.credits import payment_callback
app.add_api_route("/payment-callback", payment_callback, methods=["GET"], tags=["credits"])

@app.get("/health")
def health():
    return {"status": "ok", "service": "Applyin API"}

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error on {request.url}: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
