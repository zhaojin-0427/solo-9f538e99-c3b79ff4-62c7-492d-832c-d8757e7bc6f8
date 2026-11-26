"""Pydantic schemas for the accessible routing guidance API."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_POINTS = 40
MAX_ELEVATORS = 8


class PassageDirection(str, Enum):
    """Allowed traversal directions for a submitted passage segment."""

    ONE_WAY = "one_way"
    TWO_WAY = "two_way"


DirectionLiteral = Literal["one_way", "two_way"]
TraversalLiteral = Literal["forward", "reverse"]
SegmentKind = Literal["walking", "elevator"]
SessionStatus = Literal["in_progress", "arrived", "failed"]


_DIRECTION_ALIASES = {
    "one_way": PassageDirection.ONE_WAY,
    "one-way": PassageDirection.ONE_WAY,
    "oneway": PassageDirection.ONE_WAY,
    "forward": PassageDirection.ONE_WAY,
    "directed": PassageDirection.ONE_WAY,
    "single": PassageDirection.ONE_WAY,
    "单向": PassageDirection.ONE_WAY,
    "单向通行": PassageDirection.ONE_WAY,
    "two_way": PassageDirection.TWO_WAY,
    "two-way": PassageDirection.TWO_WAY,
    "twoway": PassageDirection.TWO_WAY,
    "both": PassageDirection.TWO_WAY,
    "bidirectional": PassageDirection.TWO_WAY,
    "round_trip": PassageDirection.TWO_WAY,
    "双向": PassageDirection.TWO_WAY,
    "双向通行": PassageDirection.TWO_WAY,
}


def _clean_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_key(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


class SegmentIn(BaseModel):
    """One submitted accessible passage segment."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    segment_id: str | None = Field(
        default=None,
        description="路段编号；不填时由服务端生成。提交的编号在同一图中不能重复。",
        examples=["seg-s-hall"],
    )
    from_point: str = Field(
        ...,
        description="进入该路段时的当前点位 ID。也兼容别名 from/source。",
        examples=["start"],
    )
    to_point: str = Field(
        ...,
        description="走完该路段后到达的点位 ID。也兼容别名 to/target。",
        examples=["elevator-hall"],
    )
    minutes: int = Field(
        ...,
        gt=0,
        description="正常通过该路段所需的正整数分钟数；电梯路段即乘梯用时。",
        examples=[2],
    )
    direction: PassageDirection = Field(
        ...,
        description=(
            "通行方向：one_way 表示仅允许 from_point → to_point；"
            "two_way 表示同一用时双向可通行。也可填写中文“单向”或“双向”。"
        ),
        examples=[PassageDirection.ONE_WAY],
    )
    elevator_id: str | None = Field(
        default=None,
        description="梯号。普通通道留空；乘梯路段填写同一台梯的编号，例如 1。",
        examples=[None],
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases_and_direction(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        item = dict(data)

        if _first_key(item, ("from_point",)) is None:
            value = _first_key(item, ("from", "source", "origin", "start_point"))
            if value is not None:
                item["from_point"] = value
        if _first_key(item, ("to_point",)) is None:
            value = _first_key(item, ("to", "target", "end_point", "destination_point"))
            if value is not None:
                item["to_point"] = value
        if item.get("segment_id") is None:
            value = _first_key(item, ("id", "edge_id"))
            if value is not None:
                item["segment_id"] = value
        if item.get("elevator_id") is None:
            value = _first_key(item, ("elevator", "elevator_no", "elevator_number", "lift_id"))
            if value is not None:
                item["elevator_id"] = value
        if "minutes" not in item:
            value = _first_key(item, ("duration_minutes", "time", "cost", "weight"))
            if value is not None:
                item["minutes"] = value

        direction = item.get("direction")
        if direction is None:
            if item.get("bidirectional") is True or item.get("two_way") is True:
                item["direction"] = PassageDirection.TWO_WAY
            elif item.get("bidirectional") is False or item.get("one_way") is True:
                item["direction"] = PassageDirection.ONE_WAY
        if isinstance(direction, str):
            normalized = _DIRECTION_ALIASES.get(direction.strip().lower())
            if normalized is None and direction.strip() in _DIRECTION_ALIASES:
                normalized = _DIRECTION_ALIASES[direction.strip()]
            if normalized is not None:
                item["direction"] = normalized

        for key in ("segment_id", "from_point", "to_point", "elevator_id"):
            if key in item:
                item[key] = _clean_id(item[key])

        return item


class GuidanceRequest(BaseModel):
    """Request to compute and start a guaranteed accessible guidance plan."""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
        json_schema_extra={
            "example": {
                "points": ["start", "e-hall", "l2", "l3", "shuttle", "ramp"],
                "start_point": "start",
                "destination_point": "shuttle",
                "available_minutes": 9,
                "segments": [
                    {
                        "segment_id": "walk-to-e1",
                        "from_point": "start",
                        "to_point": "e-hall",
                        "minutes": 1,
                        "direction": "one_way",
                    },
                    {
                        "segment_id": "e1-first-segment",
                        "from_point": "e-hall",
                        "to_point": "l2",
                        "minutes": 2,
                        "direction": "two_way",
                        "elevator_id": "1",
                    },
                    {
                        "segment_id": "e1-second-segment",
                        "from_point": "l2",
                        "to_point": "l3",
                        "minutes": 2,
                        "direction": "two_way",
                        "elevator_id": "1",
                    },
                    {
                        "segment_id": "last-walk",
                        "from_point": "l3",
                        "to_point": "shuttle",
                        "minutes": 1,
                        "direction": "one_way",
                    },
                    {
                        "segment_id": "outage-detour-1",
                        "from_point": "e-hall",
                        "to_point": "ramp",
                        "minutes": 4,
                        "direction": "one_way",
                    },
                    {
                        "segment_id": "outage-detour-2",
                        "from_point": "ramp",
                        "to_point": "shuttle",
                        "minutes": 4,
                        "direction": "one_way",
                    },
                ],
            }
        },
    )

    points: list[str] = Field(
        default_factory=list,
        description=(
            "图中的点位 ID 列表，最多 40 个。可省略；省略时从路段端点、起点和接驳点自动汇总。"
        ),
        examples=[["start", "hall", "shuttle"]],
    )
    segments: list[SegmentIn] = Field(
        ...,
        min_length=1,
        description="无障碍通道路段。每段必须给出通行方向和正整数分钟数。",
    )
    start_point: str = Field(
        ...,
        description="轮椅访客当前起点 ID。也兼容别名 start/origin。",
        examples=["start"],
    )
    destination_point: str = Field(
        ...,
        description="出口接驳点 ID。也兼容别名 destination/end/shuttle_point。",
        examples=["shuttle"],
    )
    available_minutes: int = Field(
        ...,
        ge=0,
        description="距离接驳车发车还剩多少可用分钟。",
        examples=[12],
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_top_level_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        item = dict(data)

        # Also accept a Graph-like nested object: {"graph": {"nodes": ..., "edges": ...}}.
        graph = item.get("graph")
        if isinstance(graph, dict):
            for target, keys in (
                ("points", ("points", "nodes", "vertices")),
                ("segments", ("segments", "edges")),
            ):
                if item.get(target) is None:
                    value = _first_key(graph, keys)
                    if value is not None:
                        item[target] = value

        if item.get("points") is None:
            value = _first_key(item, ("nodes", "vertices"))
            if value is not None:
                item["points"] = value
        if item.get("segments") is None:
            value = _first_key(item, ("edges",))
            if value is not None:
                item["segments"] = value
        if item.get("start_point") is None:
            value = _first_key(item, ("start", "origin", "from"))
            if value is not None:
                item["start_point"] = value
        if item.get("destination_point") is None:
            value = _first_key(
                item, ("destination", "end", "target", "shuttle_point", "exit_point")
            )
            if value is not None:
                item["destination_point"] = value
        if item.get("available_minutes") is None:
            value = _first_key(
                item,
                (
                    "budget_minutes",
                    "time_budget",
                    "remaining_minutes",
                    "minutes_available",
                    "deadline_minutes",
                ),
            )
            if value is not None:
                item["available_minutes"] = value

        if isinstance(item.get("points"), list):
            item["points"] = [_clean_id(point) for point in item["points"]]
        item["start_point"] = _clean_id(item.get("start_point"))
        item["destination_point"] = _clean_id(item.get("destination_point"))
        return item

    @model_validator(mode="after")
    def _validate_graph_limits_and_references(self) -> GuidanceRequest:
        declared_points = [point for point in self.points if point]
        if len(declared_points) != len(set(declared_points)):
            raise ValueError("points 中存在重复点位 ID")

        point_ids = set(declared_points)
        point_ids.add(self.start_point)
        point_ids.add(self.destination_point)
        for segment in self.segments:
            point_ids.add(segment.from_point)
            point_ids.add(segment.to_point)

        if len(point_ids) > MAX_POINTS:
            raise ValueError(f"无障碍通道图最多有 {MAX_POINTS} 个点位，当前有 {len(point_ids)} 个")

        if declared_points:
            declared = set(declared_points)
            missing = sorted(point_ids - declared)
            if missing:
                raise ValueError(f"以下点位未在 points 中声明: {', '.join(missing)}")

        supplied_ids = [segment.segment_id for segment in self.segments if segment.segment_id]
        if len(supplied_ids) != len(set(supplied_ids)):
            raise ValueError("路段 segment_id 不能重复")

        elevator_ids = {
            segment.elevator_id
            for segment in self.segments
            if segment.elevator_id is not None
        }
        if len(elevator_ids) > MAX_ELEVATORS:
            raise ValueError(
                f"图中最多有 {MAX_ELEVATORS} 台电梯，当前有 {len(elevator_ids)} 台: "
                + ", ".join(sorted(elevator_ids))
            )

        if self.start_point == self.destination_point and not self.segments:
            raise ValueError("起点与接驳点相同也请至少提交图结构；空图请直接不发起引路")

        return self


class ConfirmRequest(BaseModel):
    """Confirm that the currently displayed segment has been completed."""

    model_config = ConfigDict(extra="ignore")

    edge_uid: str | None = Field(
        default=None,
        description="可选。若填写，必须与当前下一步的内部有向路段 UID 一致，用于防止重复提交。",
    )
    segment_id: str | None = Field(
        default=None,
        description="可选。若填写，必须与当前下一步的业务路段编号一致。",
    )


class OutageRequest(BaseModel):
    """Report that the elevator at the current boarding point is out of service."""

    model_config = ConfigDict(extra="ignore")

    edge_uid: str | None = Field(default=None, description="可选：当前乘梯路段的内部 UID。")
    segment_id: str | None = Field(default=None, description="可选：当前乘梯路段编号。")
    elevator_id: str | None = Field(
        default=None,
        description="可选：当前准备搭乘的梯号；不填则报告当前下一步对应的电梯。",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        item = dict(data)
        if item.get("elevator_id") is None:
            value = _first_key(item, ("elevator", "elevator_no", "lift_id"))
            if value is not None:
                item["elevator_id"] = value
        item["elevator_id"] = _clean_id(item.get("elevator_id"))
        return item


class SegmentOut(BaseModel):
    """A directed segment as used by guidance."""

    edge_uid: str
    segment_id: str
    from_point: str = Field(serialization_alias="from")
    to_point: str = Field(serialization_alias="to")
    minutes: int
    kind: SegmentKind
    direction: PassageDirection
    traversal: TraversalLiteral = Field(
        description="实际遍历方向：按提交 from→to 为 forward，双向路段反向遍历时为 reverse。"
    )
    elevator_id: str | None = None
    instruction: str

    model_config = ConfigDict(populate_by_name=True)


class StepOut(BaseModel):
    """The current location and next action shown at every guidance step."""

    step_number: int
    status: str
    current_point: str
    next_segment: SegmentOut | None
    remaining_minutes: int
    elapsed_minutes: int
    can_confirm_completion: bool
    can_report_outage: bool
    allowed_actions: list[str]
    instruction: str


class ContingencyOut(BaseModel):
    """A verified outage contingency for the first boarding of one elevator."""

    elevator_id: str
    at_point: str
    edge_uid: str
    elapsed_before_boarding_minutes: int
    remaining_at_boarding_minutes: int
    fallback_minutes: int
    fallback_slack_minutes: int
    fallback_points: list[str]
    fallback_edge_uids: list[str]


class PlanResponse(BaseModel):
    """Initial planning result. No session is created when guaranteed is false."""

    guaranteed: bool
    session_id: str | None = None
    status: str
    message: str
    start_point: str
    destination_point: str
    available_minutes: int
    nominal_minutes: int | None = None
    nominal_slack_minutes: int | None = None
    path_points: list[str] = Field(default_factory=list)
    segments: list[SegmentOut] = Field(default_factory=list)
    contingencies: list[ContingencyOut] = Field(default_factory=list)
    unaffected_elevator_ids: list[str] = Field(
        default_factory=list,
        description="未出现在推荐路线中的电梯；它们停运不影响该路线。",
    )
    step: StepOut | None = None
    fastest_minutes: int | None = None
    fastest_path_points: list[str] = Field(default_factory=list)
    reason_code: str | None = None


class GuidanceResponse(BaseModel):
    """Current session state and the next guidance step."""

    session_id: str
    status: SessionStatus
    message: str
    current_point: str
    remaining_minutes: int
    elapsed_minutes: int
    reported_outage_elevator_id: str | None = None
    active_route_kind: str
    route_points: list[str]
    route_segments: list[SegmentOut]
    step: StepOut
    history: list[dict[str, Any]] = Field(default_factory=list)
