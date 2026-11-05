"""FastAPI routers."""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from ..db import get_session

# Modern FastAPI dependency idiom (keeps ruff's B008 happy).
SessionDep = Annotated[Session, Depends(get_session)]
