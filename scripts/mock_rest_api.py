from __future__ import annotations

import asyncio

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


class OrderPayload(BaseModel):
    item: dict
    payment: dict
    customer: dict


def create_mock_app() -> FastAPI:
    app = FastAPI(title="Mock REST API")
    flaky_state = {"count": 0}

    @app.get("/users/{user_id}")
    async def get_user(user_id: int, verbose: bool = False) -> dict:
        if user_id == 404:
            raise HTTPException(status_code=404, detail="User not found")
        return {
            "data": {
                "id": user_id,
                "name": f"User {user_id}",
                "email": f"user{user_id}@example.com",
                "role": "tester" if verbose else "user",
            }
        }

    @app.post("/orders")
    async def create_order(payload: OrderPayload) -> dict:
        return {
            "data": {
                "order_id": "ORD-10001",
                "status": "created",
                "amount": payload.payment.get("amount"),
                "customer": payload.customer.get("name"),
            }
        }

    @app.get("/slow")
    async def slow() -> dict:
        await asyncio.sleep(0.3)
        return {"data": {"status": "slow-ok"}}

    @app.get("/boom")
    async def boom() -> dict:
        raise HTTPException(status_code=500, detail="Mock failure")

    @app.get("/flaky")
    async def flaky() -> dict:
        flaky_state["count"] += 1
        if flaky_state["count"] % 2 == 1:
            raise HTTPException(status_code=503, detail="Temporary upstream jitter")
        return {"data": {"status": "retry-ok", "attempt": flaky_state["count"]}}

    return app


if __name__ == "__main__":
    uvicorn.run(create_mock_app(), host="127.0.0.1", port=9001)
