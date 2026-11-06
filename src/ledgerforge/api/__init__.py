"""FastAPI routers."""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from ..auth import Principal, require_role
from ..db import get_session

# Modern FastAPI dependency idiom (keeps ruff's B008 happy).
SessionDep = Annotated[Session, Depends(get_session)]

# Role gates (issue #1): reader = read-only, bookkeeper = writes.
ReaderDep = Annotated[Principal, Depends(require_role("reader"))]
BookkeeperDep = Annotated[Principal, Depends(require_role("bookkeeper"))]
