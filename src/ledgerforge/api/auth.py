"""Auth API: who am I? The smallest useful surface — the frontend and
operators use it to confirm a token is live and see the effective role."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..auth import Principal, auth_context
from ..settings import get_settings

router = APIRouter(prefix="/auth", tags=["auth"])

PrincipalDep = Annotated[Principal, Depends(auth_context)]


class MeOut(BaseModel):
    sub: str
    roles: list[str]
    auth_mode: str


@router.get("/me", response_model=MeOut)
async def me(principal: PrincipalDep) -> MeOut:
    return MeOut(
        sub=principal.sub,
        roles=list(principal.roles),
        auth_mode=get_settings().auth_mode,
    )
