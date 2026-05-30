from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, List
import hashlib, json, logging
from app.config import get_supabase, settings
from app.dependencies import get_current_user
from app.services.ai import run_analysis
from app.routers.auth import _get_balance

router = APIRouter()
logger = logging.getLogger("applyin.analyze")

ANALYSIS_CREDIT_COST = 1

class JobData(BaseModel):
    title: str
    company: str
    location: Optional[str] = ""
    description: str
    skills: List[str] = []
    experience: Optional[str] = ""

class AnalyzeRequest(BaseModel):
    job: JobData
    resume_b64: Optional[str] = None
    force_refresh: bool = False

def job_cache_key(job: JobData, resume_b64: Optional[str]) -> str:
    resume_fp = resume_b64[:60] if resume_b64 else "NO_RESUME"
    raw = f"{job.title}{job.company}{job.description[:200]}{resume_fp}"
    return hashlib.sha256(raw.encode()).hexdigest()[:40]

@router.post("/job")
async def analyze_job(req: AnalyzeRequest, user=Depends(get_current_user)):
    db = get_supabase()
    user_id = user.id

    cache_key = job_cache_key(req.job, req.resume_b64)
    logger.info(f"Analyze request: user={user_id} job='{req.job.title}' resume={'YES' if req.resume_b64 else 'NO'} force={req.force_refresh}")

    # 1. Check cache FIRST — cached results are free even with 0 credits
    if not req.force_refresh:
        try:
            cached = db.table("analysis_cache") \
                .select("result") \
                .eq("cache_key", cache_key) \
                .eq("user_id", user_id) \
                .gt("expires_at", datetime.utcnow().isoformat()) \
                .maybe_single().execute()
            if cached.data:
                balance = _get_balance(db, user_id)
                logger.info(f"Cache HIT for {user_id} — returning free cached result")
                return {**cached.data["result"], "cached": True, "credits_used": 0, "credits_remaining": balance}
        except Exception as e:
            logger.warning(f"Cache check failed (non-fatal): {e}")

    # 2. Check credits only when we need to run a fresh analysis
    balance = _get_balance(db, user_id)
    logger.info(f"Balance for {user_id}: {balance} credits")

    if balance < ANALYSIS_CREDIT_COST:
        logger.warning(f"Insufficient credits: user={user_id} balance={balance}")
        raise HTTPException(status_code=402, detail="INSUFFICIENT_CREDITS")

    # 3. Deduct credit (optimistic — refund on failure)
    db.table("credit_ledger").insert({
        "user_id": user_id,
        "delta": -ANALYSIS_CREDIT_COST,
        "reason": "analysis",
        "meta": {"job_title": req.job.title, "company": req.job.company}
    }).execute()

    try:
        # 4. Run analysis
        result = await run_analysis(req.job.dict(), req.resume_b64)
    except Exception as e:
        # Refund on error
        db.table("credit_ledger").insert({
            "user_id": user_id,
            "delta": ANALYSIS_CREDIT_COST,
            "reason": "analysis_refund",
            "meta": {"error": str(e)}
        }).execute()
        logger.error(f"Analysis failed for {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")

    # 5. Cache result for 24h
    try:
        db.table("analysis_cache").upsert({
            "cache_key": cache_key,
            "user_id": user_id,
            "result": result,
            "expires_at": (datetime.utcnow() + timedelta(hours=24)).isoformat()
        }).execute()
    except Exception as e:
        logger.warning(f"Cache write failed (non-fatal): {e}")

    # 6. Log analytics (non-fatal)
    try:
        db.table("usage_events").insert({
            "user_id": user_id,
            "job_title": req.job.title,
            "company": req.job.company,
            "match_score": result.get("match_score"),
            "fit_level": result.get("fit_level"),
            "had_resume": bool(req.resume_b64),
            "credits_used": ANALYSIS_CREDIT_COST,
        }).execute()
    except Exception as e:
        logger.warning(f"Analytics write failed (non-fatal): {e}")

    new_balance = balance - ANALYSIS_CREDIT_COST
    logger.info(f"Analysis complete: user={user_id} score={result.get('match_score')} new_balance={new_balance}")
    return {**result, "cached": False, "credits_used": ANALYSIS_CREDIT_COST, "credits_remaining": new_balance}
