"""FastAPI application for outage-safe accessible route guidance."""

from __future__ import annotations

from fastapi import Body, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from .models import (
    ConfirmRequest,
    GuidanceRequest,
    GuidanceResponse,
    OutageRequest,
    PlanResponse,
)
from .service import (
    SessionError,
    confirm_completion,
    create_guidance,
    read_guidance,
    report_outage,
)

tags_metadata = [
    {
        "name": "planning",
        "description": "提交无障碍通道图并计算停运保证方案。",
    },
    {
        "name": "guidance",
        "description": "引路会话中的逐步确认、电梯停运报告和状态查询。",
    },
    {
        "name": "system",
        "description": "健康检查。",
    },
]

app = FastAPI(
    title="轮椅访客接驳车引路 API",
    version="1.0.0",
    description=(
        "在 `/docs` 提交无障碍通道图、起点、出口接驳点和可用分钟数。\n\n"
        "规划器在 `(点位, 已乘坐电梯集合)` 状态图上运行最短路：首次准备搭乘某台电梯时，"
        "只有当该梯所有路段不可用后的最短绕行能放进剩余时间，才允许选择该乘梯段。"
        "因此返回的方案保证至多一台电梯停运时仍可准时到达；在所有满足保证的方案中，"
        "无停运时的总用时最短。\n\n"
        "无解时返回 `guaranteed=false` 且不创建会话。沿途用 confirm 确认走完当前路段；"
        "首次在梯口准备搭乘时可用 outage 报告停运。已成功乘过的梯不能再报停运。"
    ),
    openapi_tags=tags_metadata,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(SessionError)
async def session_error_handler(_request: Request, error: SessionError) -> JSONResponse:
    detail = {
        "code": error.code,
        "message": error.message,
    }
    detail.update(error.extra)
    return JSONResponse(status_code=error.status_code, content={"detail": detail})


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["system"], summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/api/guidance/plan",
    response_model=PlanResponse,
    tags=["planning"],
    summary="提交通道图并开始有保证的引路规划",
)
def plan_route(request: GuidanceRequest) -> PlanResponse:
    """Create a session only when an all-outage guaranteed route exists."""

    return create_guidance(request)


@app.get(
    "/api/sessions/{session_id}",
    response_model=GuidanceResponse,
    tags=["guidance"],
    summary="查看当前位置、下一段和剩余分钟数",
)
def read_session(session_id: str) -> GuidanceResponse:
    return read_guidance(session_id)


@app.post(
    "/api/sessions/{session_id}/confirm",
    response_model=GuidanceResponse,
    tags=["guidance"],
    summary="确认走完当前路段",
)
def confirm_segment(
    session_id: str,
    payload: ConfirmRequest | None = Body(default=None),
) -> GuidanceResponse:
    return confirm_completion(session_id, payload)


@app.post(
    "/api/sessions/{session_id}/outage",
    response_model=GuidanceResponse,
    tags=["guidance"],
    summary="在当前梯口准备搭乘时报告该梯停运",
)
def report_current_outage(
    session_id: str,
    payload: OutageRequest | None = Body(default=None),
) -> GuidanceResponse:
    return report_outage(session_id, payload)
