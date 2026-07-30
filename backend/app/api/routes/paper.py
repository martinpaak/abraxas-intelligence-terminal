from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.app.storage.paper import account_snapshot, place_market_order, reconcile_paper_runtime, reset_account, set_bot_circuit_breaker, set_bot_runtime_state, update_position_protection
from backend.app.storage.paper_sessions import change_paper_bot_session, list_paper_bot_sessions, run_due_paper_bot_sessions, run_paper_bot_session, start_paper_bot_session

router = APIRouter(prefix="/api/paper", tags=["paper-trading"])


class PaperOrderRequest(BaseModel):
    symbol: str = Field(min_length=2, max_length=30)
    side: str = Field(pattern="^(buy|sell)$")
    quantity: float = Field(gt=0, le=1_000_000_000)
    bot_id: int | None = Field(default=None, ge=1)


class PaperResetRequest(BaseModel):
    initial_balance: float = Field(default=10_000, gt=0, le=1_000_000_000)
    reason: str = Field(min_length=3, max_length=500)


class PaperProtectionRequest(BaseModel):
    stop_loss_price: float | None = Field(default=None, gt=0)
    take_profit_price: float | None = Field(default=None, gt=0)
    trailing_distance_pct: float | None = Field(default=None, gt=0, le=50)


class PaperBotRuntimeRequest(BaseModel):
    status: str = Field(pattern="^(active|paused)$")
    reason: str = Field(min_length=3, max_length=500)
    pause_minutes: int | None = Field(default=None, ge=1, le=43_200)


class PaperBotCircuitBreakerRequest(BaseModel):
    enabled: bool = True
    max_consecutive_losses: int = Field(default=3, ge=1, le=100)
    max_rejections: int = Field(default=5, ge=1, le=1000)
    rejection_window_minutes: int = Field(default=15, ge=1, le=1440)
    max_drawdown_pct: float = Field(default=10, gt=0, le=100)
    pause_minutes: int = Field(default=60, ge=1, le=43_200)


class PaperBotSessionStartRequest(BaseModel):
    bot_id: int = Field(ge=1)
    version_id: int | None = Field(default=None, ge=1)
    cadence_seconds: int = Field(default=300, ge=60, le=86_400)
    execution_mode: str = Field(default="observe", pattern="^(observe|auto_paper)$")


class PaperBotSessionActionRequest(BaseModel):
    action: str = Field(pattern="^(pause|resume|stop)$")
    reason: str = Field(min_length=3, max_length=500)


def values(model: BaseModel) -> dict:
    return model.model_dump() if hasattr(model, "model_dump") else model.dict()


@router.get("")
def paper_account() -> dict:
    return account_snapshot()


@router.post("/orders")
def paper_order(payload: PaperOrderRequest) -> dict:
    try:
        return place_market_order(values(payload))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/reset")
def paper_reset(payload: PaperResetRequest) -> dict:
    data = values(payload)
    return reset_account(data["initial_balance"], data["reason"])


@router.post("/reconcile")
def paper_reconcile() -> dict:
    return reconcile_paper_runtime()


@router.get("/sessions")
def paper_sessions(bot_id: int | None = None, limit: int = 100) -> dict:
    return list_paper_bot_sessions(bot_id=bot_id, limit=max(1, min(int(limit), 500)))


@router.post("/sessions")
def paper_session_start(payload: PaperBotSessionStartRequest) -> dict:
    try:
        return start_paper_bot_session(**values(payload))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sessions/run-due")
def paper_sessions_run_due(limit: int = 20) -> dict:
    return run_due_paper_bot_sessions(limit=limit)


@router.post("/sessions/{session_id}/run")
def paper_session_run(session_id: int) -> dict:
    try:
        return run_paper_bot_session(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/sessions/{session_id}")
def paper_session_action(payload: PaperBotSessionActionRequest, session_id: int) -> dict:
    try:
        return change_paper_bot_session(session_id, **values(payload))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/bots/{bot_id}/runtime")
def paper_bot_runtime(payload: PaperBotRuntimeRequest, bot_id: int) -> dict:
    try:
        return set_bot_runtime_state(bot_id, **values(payload))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/bots/{bot_id}/circuit-breaker")
def paper_bot_circuit_breaker(payload: PaperBotCircuitBreakerRequest, bot_id: int) -> dict:
    try:
        return set_bot_circuit_breaker(bot_id, values(payload))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/allocations/{allocation_id}/protection")
def paper_protection(payload: PaperProtectionRequest, allocation_id: int) -> dict:
    try:
        return update_position_protection(allocation_id, **values(payload))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
