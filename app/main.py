from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from app.routers import auth, analyze, credits, webhook
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fitly")

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="Fitly API",
    version="1.0.0",
    docs_url="/docs",       # disable in prod: docs_url=None
    redoc_url=None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.middleware("http")
async def fix_double_slash(request: Request, call_next):
    # Fix double slashes in URL path (e.g. //credits/... → /credits/...)
    if "//" in request.url.path:
        fixed = request.url.path.replace("//", "/")
        url = str(request.url).replace(request.url.path, fixed)
        return RedirectResponse(url=url, status_code=301)
    return await call_next(request)

# CORS — lock to your extension ID in production
# Chrome extension origin format: chrome-extension://<id>
ALLOWED_ORIGINS = [
    "https://www.linkedin.com",     # content script origin
    "chrome-extension://",          # all extension origins (lock down after publishing)
]

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"chrome-extension://.*|https://www\.linkedin\.com",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    allow_credentials=True,
)

app.include_router(auth.router,     prefix="/auth",     tags=["auth"])
app.include_router(analyze.router,  prefix="/analyze",  tags=["analyze"])
app.include_router(credits.router,  prefix="/credits",  tags=["credits"])
app.include_router(webhook.router,  prefix="/webhook",  tags=["webhook"])

# Also mount callback at root to handle trailing-slash edge cases
from app.routers.credits import payment_callback
app.add_api_route("/payment-callback", payment_callback, methods=["GET"], tags=["credits"])

@app.get("/health")
def health():
    return {"status": "ok", "service": "Fitly API"}

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
