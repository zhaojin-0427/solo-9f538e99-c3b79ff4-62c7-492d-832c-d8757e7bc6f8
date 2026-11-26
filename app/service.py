"""In-memory session store and guidance state transitions."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from .models import (
    ConfirmRequest,
    ContingencyOut,
    GuidanceRequest,
    GuidanceResponse,
    OutageRequest,
    PlanResponse,
    SegmentOut,
    StepOut,
)
from .planner import (
    Edge,
    Graph,
    NoGuaranteedRoute,
    PlannedRoute,
    build_graph,
    plan_guaranteed_route,
    shortest_path,
)


class SessionError(Exception):
    """An invalid session action or missing session."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}


@dataclass
class GuidanceSession:
    session_id: str
    request: GuidanceRequest
    graph: Graph
    destination: str
    budget_minutes: int
    current_point: str
    edge_uids: list[str]
    plan: PlannedRoute
    elapsed_minutes: int = 0
    completed_count: int = 0
    route_kind: str = "nominal"
    ridden_elevators: set[str] = field(default_factory=set)
    reported_outage_elevator_id: str | None = None
    status: str = "in_progress"
    history: list[dict[str, Any]] = field(default_factory=list)


SESSIONS: dict[str, GuidanceSession] = {}
STORE_LOCK = threading.RLock()


def edge_to_output(edge: Edge) -> SegmentOut:
    if edge.elevator_id is not None:
        instruction = (
            f"前往 {edge.source} 的 {edge.elevator_id} 号梯口并准备搭乘；"
            f"乘梯到 {edge.target}，乘梯用时 {edge.minutes} 分钟。"
            "若准备搭乘时发现该梯停运，请调用 outage 报告；"
            "若电梯可用并已走完该路段，请调用 confirm 确认。"
        )
        kind = "elevator"
    else:
        instruction = (
            f"沿无障碍通道从 {edge.source} 行至 {edge.target}，"
            f"预计 {edge.minutes} 分钟；走完后调用 confirm 确认。"
        )
        kind = "walking"

    return SegmentOut(
        edge_uid=edge.uid,
        segment_id=edge.segment_id,
        from_point=edge.source,
        to_point=edge.target,
        minutes=edge.minutes,
        kind=kind,
        direction=edge.submitted_direction,  # type: ignore[arg-type]
        traversal=edge.traversal,  # type: ignore[arg-type]
        elevator_id=edge.elevator_id,
        instruction=instruction,
    )


def points_for_edges(start_point: str, edges: list[Edge]) -> list[str]:
    if not edges:
        return [start_point]
    points = [edges[0].source]
    for edge in edges:
        points.append(edge.target)
    return points


def get_session(session_id: str) -> GuidanceSession:
    with STORE_LOCK:
        session = SESSIONS.get(session_id)
    if session is None:
        raise SessionError(404, "SESSION_NOT_FOUND", "引路会话不存在或已因服务重启清空。")
    return session


def current_edge(session: GuidanceSession) -> Edge | None:
    if not session.edge_uids:
        return None
    return session.graph.by_uid[session.edge_uids[0]]


def can_report_outage(session: GuidanceSession, edge: Edge | None) -> bool:
    return (
        session.status == "in_progress"
        and edge is not None
        and edge.elevator_id is not None
        and session.reported_outage_elevator_id is None
        and edge.elevator_id not in session.ridden_elevators
    )


def build_step(session: GuidanceSession) -> StepOut:
    edge = current_edge(session)
    remaining = session.budget_minutes - session.elapsed_minutes

    can_confirm = session.status == "in_progress" and edge is not None
    can_outage = can_report_outage(session, edge)
    actions: list[str] = []
    if can_confirm:
        actions.append("confirm_completion")
    if can_outage:
        actions.append("report_elevator_outage")

    if session.status == "arrived":
        instruction = (
            f"已到达接驳点 {session.destination}，累计用时 "
            f"{session.elapsed_minutes} 分钟，剩余 {max(remaining, 0)} 分钟。引路结束。"
        )
        status = "arrived"
    elif session.status == "failed":
        instruction = (
            "引路未能继续保证准时到达；请按现场工作人员指引处理。"
            f"当前位置 {session.current_point}，剩余 {remaining} 分钟。"
        )
        status = "failed"
    elif edge is None:
        instruction = f"当前位置 {session.current_point} 已没有后续路段。"
        status = session.status
    elif edge.elevator_id is not None:
        if edge.elevator_id in session.ridden_elevators:
            instruction = (
                f"当前位置 {session.current_point}。下一段是 {edge.elevator_id} 号梯的"
                f"同梯分段/再次搭乘，前往 {edge.target}，乘梯 {edge.minutes} 分钟。"
                "该梯此前已成功乘坐，按规则不能再报告停运；请完成后确认。"
                f"当前剩余 {remaining} 分钟。"
            )
        else:
            instruction = (
                f"当前位置 {session.current_point}。下一段：在 {edge.source} 的 "
                f"{edge.elevator_id} 号梯准备搭乘，到 {edge.target}，"
                f"{edge.minutes} 分钟。当前剩余 {remaining} 分钟。"
                "走到梯口后若发现停运，请先调用 outage，乘梯时间不会扣除；"
                "若已正常走完，请调用 confirm。"
            )
        status = session.status
    else:
        instruction = (
            f"当前位置 {session.current_point}。下一段：从 {edge.source} 到 "
            f"{edge.target}，步行/通行 {edge.minutes} 分钟。"
            f"当前剩余 {remaining} 分钟；走完后调用 confirm。"
        )
        status = session.status

    return StepOut(
        step_number=session.completed_count + 1,
        status=status,
        current_point=session.current_point,
        next_segment=edge_to_output(edge) if edge else None,
        remaining_minutes=remaining,
        elapsed_minutes=session.elapsed_minutes,
        can_confirm_completion=can_confirm,
        can_report_outage=can_outage,
        allowed_actions=actions,
        instruction=instruction,
    )


def guidance_response(
    session: GuidanceSession,
    message: str,
) -> GuidanceResponse:
    edges = [session.graph.by_uid[uid] for uid in session.edge_uids]
    return GuidanceResponse(
        session_id=session.session_id,
        status=session.status,  # type: ignore[arg-type]
        message=message,
        current_point=session.current_point,
        remaining_minutes=session.budget_minutes - session.elapsed_minutes,
        elapsed_minutes=session.elapsed_minutes,
        reported_outage_elevator_id=session.reported_outage_elevator_id,
        active_route_kind=session.route_kind,
        route_points=points_for_edges(session.current_point, edges),
        route_segments=[edge_to_output(edge) for edge in edges],
        step=build_step(session),
        history=session.history,
    )


def contingency_to_output(contingency: Any) -> ContingencyOut:
    return ContingencyOut(
        elevator_id=contingency.elevator_id,
        at_point=contingency.at_point,
        edge_uid=contingency.edge_uid,
        elapsed_before_boarding_minutes=contingency.elapsed_before,
        remaining_at_boarding_minutes=contingency.remaining_at_boarding,
        fallback_minutes=contingency.fallback_minutes,
        fallback_slack_minutes=(
            contingency.remaining_at_boarding - contingency.fallback_minutes
        ),
        fallback_points=points_for_edges(
            contingency.at_point, contingency.fallback_edges
        ),
        fallback_edge_uids=[edge.uid for edge in contingency.fallback_edges],
    )


def create_guidance(request: GuidanceRequest) -> PlanResponse:
    """Plan and, only when guaranteed, create an in-memory guidance session."""

    graph = build_graph(request)

    try:
        planned = plan_guaranteed_route(
            graph,
            request.start_point,
            request.destination_point,
            request.available_minutes,
        )
    except NoGuaranteedRoute as error:
        fastest_points: list[str] = []
        if error.fastest_edges:
            fastest_points = points_for_edges(
                request.start_point, error.fastest_edges
            )
        return PlanResponse(
            guaranteed=False,
            session_id=None,
            status="not_started",
            message=error.message,
            start_point=request.start_point,
            destination_point=request.destination_point,
            available_minutes=request.available_minutes,
            fastest_minutes=error.fastest_minutes,
            fastest_path_points=fastest_points,
            reason_code=error.reason_code,
        )

    session_id = uuid.uuid4().hex
    session = GuidanceSession(
        session_id=session_id,
        request=request,
        graph=graph,
        destination=request.destination_point,
        budget_minutes=request.available_minutes,
        current_point=request.start_point,
        edge_uids=[edge.uid for edge in planned.edges],
        plan=planned,
        status="arrived" if request.start_point == request.destination_point else "in_progress",
        history=[
            {
                "type": "plan_created",
                "nominal_minutes": planned.total_minutes,
                "available_minutes": request.available_minutes,
                "contingency_elevators": [
                    item.elevator_id for item in planned.contingencies
                ],
            }
        ],
    )
    if session.status == "arrived":
        session.history.append({"type": "arrived", "at_point": session.current_point})

    with STORE_LOCK:
        SESSIONS[session_id] = session

    step = build_step(session)
    contingency_ids = {item.elevator_id for item in planned.contingencies}
    unaffected_elevator_ids = [
        elevator_id
        for elevator_id in graph.elevator_ids
        if elevator_id not in contingency_ids
    ]
    message = (
        f"已找到保证方案：无停运时用时 {planned.total_minutes} 分钟，"
        f"可用 {request.available_minutes} 分钟。每个首次搭乘的电梯口均已验证绕行方案。"
        "请按 step 指引逐步确认。"
    )
    if session.status == "arrived":
        message = "起点与接驳点相同，无需引路；当前已在接驳点。"

    return PlanResponse(
        guaranteed=True,
        session_id=session_id,
        status=session.status,
        message=message,
        start_point=request.start_point,
        destination_point=request.destination_point,
        available_minutes=request.available_minutes,
        nominal_minutes=planned.total_minutes,
        nominal_slack_minutes=request.available_minutes - planned.total_minutes,
        path_points=points_for_edges(request.start_point, planned.edges),
        segments=[edge_to_output(edge) for edge in planned.edges],
        contingencies=[
            contingency_to_output(item) for item in planned.contingencies
        ],
        unaffected_elevator_ids=unaffected_elevator_ids,
        step=step,
    )


def read_guidance(session_id: str) -> GuidanceResponse:
    with STORE_LOCK:
        session = get_session(session_id)
        return guidance_response(session, "当前引路状态。")


def confirm_completion(
    session_id: str,
    payload: ConfirmRequest | None = None,
) -> GuidanceResponse:
    payload = payload or ConfirmRequest()

    with STORE_LOCK:
        session = get_session(session_id)

        if session.status == "arrived":
            raise SessionError(
                409,
                "SESSION_ALREADY_ARRIVED",
                "会话已到达接驳点，不能再确认路段。",
            )
        if session.status == "failed":
            raise SessionError(
                409,
                "SESSION_FAILED",
                "会话已失去准时保证，不能继续确认引路。",
            )

        edge = current_edge(session)
        if edge is None:
            session.status = "failed"
            raise SessionError(
                409,
                "NO_NEXT_SEGMENT",
                "当前没有可确认的下一路段，但尚未记录到达接驳点。",
            )

        if payload.edge_uid and payload.edge_uid != edge.uid:
            raise SessionError(
                409,
                "STALE_EDGE_UID",
                f"提交的 edge_uid={payload.edge_uid} 不是当前路段 {edge.uid}。",
                extra={"current_edge_uid": edge.uid},
            )
        if payload.segment_id and payload.segment_id != edge.segment_id:
            raise SessionError(
                409,
                "STALE_SEGMENT_ID",
                f"提交的 segment_id={payload.segment_id} 不是当前路段 {edge.segment_id}。",
                extra={"current_segment_id": edge.segment_id},
            )

        session.elapsed_minutes += edge.minutes
        session.current_point = edge.target
        session.completed_count += 1
        if edge.elevator_id is not None:
            session.ridden_elevators.add(edge.elevator_id)

        session.edge_uids.pop(0)
        event: dict[str, Any] = {
            "type": "segment_completed",
            "edge_uid": edge.uid,
            "segment_id": edge.segment_id,
            "from_point": edge.source,
            "to_point": edge.target,
            "minutes_deducted": edge.minutes,
            "elapsed_minutes": session.elapsed_minutes,
            "remaining_minutes": session.budget_minutes - session.elapsed_minutes,
        }
        if edge.elevator_id is not None:
            event["elevator_id"] = edge.elevator_id
            event["elevator_now_known_working"] = True
        session.history.append(event)

        if not session.edge_uids:
            if session.current_point == session.destination:
                session.status = "arrived"
                session.history.append(
                    {
                        "type": "arrived",
                        "at_point": session.current_point,
                        "elapsed_minutes": session.elapsed_minutes,
                        "remaining_minutes": session.budget_minutes
                        - session.elapsed_minutes,
                    }
                )
                message = "已确认最后一段完成，到达出口接驳点。"
            else:
                session.status = "failed"
                message = "路线数据异常：路段已走完但未到达接驳点。"
        else:
            remaining = session.budget_minutes - session.elapsed_minutes
            if remaining < 0:
                session.status = "failed"
                message = "用时已超过可用分钟数，无法保证赶上接驳车。"
            else:
                message = "已确认走完当前路段，并已更新当前位置和剩余分钟数。"

        return guidance_response(session, message)


def report_outage(
    session_id: str,
    payload: OutageRequest | None = None,
) -> GuidanceResponse:
    payload = payload or OutageRequest()

    with STORE_LOCK:
        session = get_session(session_id)

        if session.status != "in_progress":
            raise SessionError(
                409,
                "SESSION_NOT_IN_PROGRESS",
                f"会话状态为 {session.status}，不能报告停运。",
            )

        edge = current_edge(session)
        if edge is None:
            raise SessionError(409, "NO_NEXT_SEGMENT", "当前没有准备搭乘的电梯路段。")

        if payload.edge_uid and payload.edge_uid != edge.uid:
            raise SessionError(
                409,
                "STALE_EDGE_UID",
                f"提交的 edge_uid={payload.edge_uid} 不是当前路段 {edge.uid}。",
                extra={"current_edge_uid": edge.uid},
            )
        if payload.segment_id and payload.segment_id != edge.segment_id:
            raise SessionError(
                409,
                "STALE_SEGMENT_ID",
                f"提交的 segment_id={payload.segment_id} 不是当前路段 {edge.segment_id}。",
                extra={"current_segment_id": edge.segment_id},
            )

        elevator_id = edge.elevator_id
        if elevator_id is None:
            raise SessionError(
                409,
                "NOT_AN_ELEVATOR_SEGMENT",
                "当前下一步是普通通道，不是准备搭乘电梯，不能报告电梯停运。",
                extra={"current_edge_uid": edge.uid},
            )

        if payload.elevator_id and payload.elevator_id != elevator_id:
            raise SessionError(
                409,
                "OUTAGE_ELEVATOR_MISMATCH",
                f"当前准备搭乘的是 {elevator_id} 号梯，不是 {payload.elevator_id} 号梯。",
                extra={"current_elevator_id": elevator_id},
            )

        if session.reported_outage_elevator_id is not None:
            raise SessionError(
                409,
                "OUTAGE_ALREADY_REPORTED",
                f"本程已报告 {session.reported_outage_elevator_id} 号梯停运；"
                "一程至多一台梯停运，请按既有绕行路线继续。",
                extra={
                    "reported_outage_elevator_id": session.reported_outage_elevator_id
                },
            )

        if elevator_id in session.ridden_elevators:
            raise SessionError(
                409,
                "ELEVATOR_ALREADY_USED",
                f"{elevator_id} 号梯此前已成功乘坐，状态全程不变，不能再报告该梯停运。",
                extra={"ridden_elevator_ids": sorted(session.ridden_elevators)},
            )

        remaining = session.budget_minutes - session.elapsed_minutes
        fallback_minutes, fallback_edges = shortest_path(
            session.graph,
            session.current_point,
            session.destination,
            excluded_elevator_id=elevator_id,
        )

        if fallback_minutes is None or fallback_minutes > remaining:
            # This should be unreachable for a session created by the guaranteed planner.
            session.status = "failed"
            session.history.append(
                {
                    "type": "outage_without_guaranteed_fallback",
                    "elevator_id": elevator_id,
                    "at_point": session.current_point,
                    "remaining_minutes": remaining,
                    "fallback_minutes": fallback_minutes,
                }
            )
            raise SessionError(
                409,
                "NO_TIME_FOR_FALLBACK",
                f"{elevator_id} 号梯停运后，从当前位置用剩余 {remaining} 分钟"
                + (
                    "找不到绕行路线。"
                    if fallback_minutes is None
                    else f"最短绕行仍需 {fallback_minutes} 分钟。"
                ),
                extra={
                    "remaining_minutes": remaining,
                    "fallback_minutes": fallback_minutes,
                },
            )

        session.reported_outage_elevator_id = elevator_id
        session.route_kind = f"fallback_without_elevator_{elevator_id}"
        session.edge_uids = [item.uid for item in fallback_edges]
        session.history.append(
            {
                "type": "elevator_outage_reported",
                "elevator_id": elevator_id,
                "at_point": session.current_point,
                "edge_uid": edge.uid,
                "elevator_minutes_not_deducted": edge.minutes,
                "elapsed_minutes": session.elapsed_minutes,
                "remaining_minutes": remaining,
                "fallback_minutes": fallback_minutes,
                "fallback_edge_uids": [item.uid for item in fallback_edges],
            }
        )

        return guidance_response(
            session,
            f"已记录 {elevator_id} 号梯停运。您留在 {session.current_point}，"
            f"乘梯 {edge.minutes} 分钟未扣除，剩余 {remaining} 分钟；"
            f"已切换为避开 {elevator_id} 号梯全部路段、用时 {fallback_minutes} 分钟的绕行路线。",
        )
