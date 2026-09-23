from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.api.schemas import LoginIn, UserOut
from backend.audit.service import audit
from backend.core.db import get_db
from backend.metadata.models import User
from backend.security.auth import create_token, get_current_user, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if not user or not verify_password(body.password, user.password_hash) or not user.is_active:
        raise HTTPException(401, "Invalid credentials")
    audit(db, user, "login", ip=request.client.host if request.client else None)
    db.commit()
    return {"token": create_token(user), "user": UserOut.model_validate(user)}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user
