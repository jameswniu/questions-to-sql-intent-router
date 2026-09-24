from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await db.close_all()


app = FastAPI(title="claims-qa", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}
