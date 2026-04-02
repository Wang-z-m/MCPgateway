from __future__ import annotations

from fastapi import APIRouter, Request


router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict[str, object]:
    registry = request.app.state.registry
    return {
        "status": "ok",
        "gateway": registry.status(),
    }
