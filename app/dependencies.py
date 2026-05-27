from fastapi import HTTPException, Header
from app.config import get_supabase
import jwt

async def get_current_user(authorization: str = Header(...)):
    """Validate Supabase JWT and return user_id."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization.replace("Bearer ", "")
    try:
        db = get_supabase()
        # Use Supabase to verify the token
        user = db.auth.get_user(token)
        if not user or not user.user:
            raise HTTPException(status_code=401, detail="Invalid token")
        return user.user
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Auth failed: {str(e)}")
