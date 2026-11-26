"""Pure routing logic for outage-safe wheelchair guidance.

The planner searches an augmented state graph whose state is
``(current_point, ridden_elevator_mask)``.  Before the first ride of elevator
``e``, the transition is accepted only when an ``e``-free route to the shuttle
point fits in the time remaining at the elevator entrance.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from .models import GuidanceRequest, MAX_ELEVATORS


@dataclass(frozen=True)
class Edge:
    """A directed traversal edge. A two-way segment creates two Edge objects."""

    uid: str
    segment_id: str
    source: str
    target: str
    minutes: int
    elevator_id: str | None
    submitted_direction: str
    traversal: str  # forward | reverse


@dataclass
class Graph:
    points: tuple[str, ...]
    edges: list[Edge]
    by_uid: dict[str, Edge]
    outgoing: dict[str, list[Edge]]
    elevator_ids: tuple[str, ...]
    elevator_index: dict[str, int]


@dataclass
class Contingency:
    elevator_id: str
    at_point: str
    edge_uid: str
    elapsed_before: int
    remaining_at_boarding: int
    fallback_minutes: int
    fallback_edges: list[Edge] = field(default_factory=list)


@dataclass
class PlannedRoute:
    edges: list[Edge]
    total_minutes: int
    contingencies: list[Contingency]


class NoGuaranteedRoute(Exception):
    """Raised when no route satisfies the all-outage guarantee within budget."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        fastest_minutes: int | None = None,
        fastest_edges: list[Edge] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.reason_code = reason_code
        self.fastest_minutes = fastest_minutes
        self.fastest_edges = fastest_edges or []


def build_graph(request: GuidanceRequest) -> Graph:
    """Convert the request's segments into a directed multigraph."""

    edges: list[Edge] = []
    by_uid: dict[str, Edge] = {}
    outgoing: dict[str, list[Edge]] = {}
    elevator_ids: list[str] = []

    def add_edge(
        *,
        uid: str,
        segment_id: str,
        source: str,
        target: str,
        minutes: int,
        elevator_id: str | None,
        submitted_direction: str,
        traversal: str,
    ) -> None:
        edge = Edge(
            uid=uid,
            segment_id=segment_id,
            source=source,
            target=target,
            minutes=minutes,
            elevator_id=elevator_id,
            submitted_direction=submitted_direction,
            traversal=traversal,
        )
        if uid in by_uid:
            # UIDs are generated from unique segment indexes; this guards future changes.
            raise RuntimeError(f"duplicate internal edge uid: {uid}")
        edges.append(edge)
        by_uid[uid] = edge
        outgoing.setdefault(source, []).append(edge)
        outgoing.setdefault(target, [])

    for index, segment in enumerate(request.segments, start=1):
        segment_id = segment.segment_id or f"seg-{index}"
        base_uid = f"edge-{index}"
        direction_value = (
            segment.direction.value
            if hasattr(segment.direction, "value")
            else str(segment.direction)
        )

        add_edge(
            uid=f"{base_uid}-forward",
            segment_id=segment_id,
            source=segment.from_point,
            target=segment.to_point,
            minutes=segment.minutes,
            elevator_id=segment.elevator_id,
            submitted_direction=direction_value,
            traversal="forward",
        )

        if segment.direction.value == "two_way":
            add_edge(
                uid=f"{base_uid}-reverse",
                segment_id=segment_id,
                source=segment.to_point,
                target=segment.from_point,
                minutes=segment.minutes,
                elevator_id=segment.elevator_id,
                submitted_direction=direction_value,
                traversal="reverse",
            )

        if segment.elevator_id and segment.elevator_id not in elevator_ids:
            elevator_ids.append(segment.elevator_id)

    points: set[str] = {request.start_point, request.destination_point}
    if request.points:
        points.update(request.points)
    for segment in request.segments:
        points.add(segment.from_point)
        points.add(segment.to_point)

    for node_edges in outgoing.values():
        node_edges.sort(key=lambda edge: (edge.minutes, edge.uid))

    elevator_index = {elevator_id: i for i, elevator_id in enumerate(elevator_ids)}
    if len(elevator_ids) > MAX_ELEVATORS:
        # The request validator normally catches this before planner construction.
        raise ValueError(f"at most {MAX_ELEVATORS} elevators are supported")

    return Graph(
        points=tuple(sorted(points)),
        edges=edges,
        by_uid=by_uid,
        outgoing=outgoing,
        elevator_ids=tuple(elevator_ids),
        elevator_index=elevator_index,
    )


def shortest_path(
    graph: Graph,
    start: str,
    destination: str,
    *,
    excluded_elevator_id: str | None = None,
) -> tuple[int | None, list[Edge]]:
    """Return shortest duration and directed edges, optionally excluding one elevator."""

    if start == destination:
        return 0, []

    distances: dict[str, int] = {start: 0}
    previous: dict[str, tuple[str, Edge]] = {}
    counter = 0
    heap: list[tuple[int, int, str]] = [(0, counter, start)]

    while heap:
        current_cost, _, node = heapq.heappop(heap)
        if current_cost != distances.get(node):
            continue
        if node == destination:
            break

        for edge in graph.outgoing.get(node, []):
            if excluded_elevator_id and edge.elevator_id == excluded_elevator_id:
                continue
            new_cost = current_cost + edge.minutes
            if new_cost < distances.get(edge.target, 10**30):
                distances[edge.target] = new_cost
                previous[edge.target] = (node, edge)
                counter += 1
                heapq.heappush(heap, (new_cost, counter, edge.target))

    if destination not in distances:
        return None, []

    edges: list[Edge] = []
    node = destination
    while node != start:
        previous_node, edge = previous[node]
        edges.append(edge)
        node = previous_node
    edges.reverse()
    return distances[destination], edges


def distances_to_destination_excluding(
    graph: Graph, destination: str, excluded_elevator_id: str
) -> dict[str, int]:
    """Compute shortest u -> destination distances without the excluded elevator."""

    reverse_outgoing: dict[str, list[tuple[str, Edge]]] = {}
    for edge in graph.edges:
        if edge.elevator_id == excluded_elevator_id:
            continue
        # Original edge is edge.source -> edge.target. In reverse search, from
        # edge.target we can reach edge.source with the same weight.
        reverse_outgoing.setdefault(edge.target, []).append((edge.source, edge))

    distances: dict[str, int] = {destination: 0}
    counter = 0
    heap: list[tuple[int, int, str]] = [(0, counter, destination)]

    while heap:
        current_cost, _, node = heapq.heappop(heap)
        if current_cost != distances.get(node):
            continue
        for neighbor, _original_edge in reverse_outgoing.get(node, []):
            new_cost = current_cost + _original_edge.minutes
            if new_cost < distances.get(neighbor, 10**30):
                distances[neighbor] = new_cost
                counter += 1
                heapq.heappush(heap, (new_cost, counter, neighbor))

    return distances


def build_contingencies(
    graph: Graph,
    destination: str,
    edges: list[Edge],
    budget: int,
) -> list[Contingency]:
    contingencies: list[Contingency] = []
    elapsed = 0
    ridden: set[str] = set()

    for edge in edges:
        elevator_id = edge.elevator_id
        if elevator_id and elevator_id not in ridden:
            fallback_minutes, fallback_edges = shortest_path(
                graph,
                edge.source,
                destination,
                excluded_elevator_id=elevator_id,
            )
            if fallback_minutes is None:
                raise NoGuaranteedRoute(
                    f"{elevator_id} 号梯在 {edge.source} 停运时无绕行路线",
                    reason_code="NO_FALLBACK_PATH",
                )
            if elapsed + fallback_minutes > budget:
                raise NoGuaranteedRoute(
                    f"{elevator_id} 号梯在 {edge.source} 停运时绕行时间不足",
                    reason_code="FALLBACK_TOO_SLOW",
                )

            contingencies.append(
                Contingency(
                    elevator_id=elevator_id,
                    at_point=edge.source,
                    edge_uid=edge.uid,
                    elapsed_before=elapsed,
                    remaining_at_boarding=budget - elapsed,
                    fallback_minutes=fallback_minutes,
                    fallback_edges=fallback_edges,
                )
            )
            # A successful ride is assumed in the no-outage trajectory.
            ridden.add(elevator_id)

        elapsed += edge.minutes
        if elevator_id:
            ridden.add(elevator_id)

    return contingencies


def plan_guaranteed_route(
    graph: Graph,
    start: str,
    destination: str,
    budget_minutes: int,
) -> PlannedRoute:
    """Find the shortest no-outage route among all outage-guaranteed plans."""

    fastest_minutes, fastest_edges = shortest_path(graph, start, destination)
    if fastest_minutes is None:
        raise NoGuaranteedRoute(
            "无障碍通道图中不存在从起点到接驳点的路径，无法保证赶上接驳车。",
            reason_code="NO_PATH",
        )
    if fastest_minutes > budget_minutes:
        raise NoGuaranteedRoute(
            f"即使没有电梯停运，最短路线也需要 {fastest_minutes} 分钟，"
            f"超过可用的 {budget_minutes} 分钟，无法保证赶上接驳车。",
            reason_code="FASTEST_TOO_SLOW",
            fastest_minutes=fastest_minutes,
            fastest_edges=fastest_edges,
        )

    fallback_distances: dict[str, dict[str, int]] = {}
    for elevator_id in graph.elevator_ids:
        fallback_distances[elevator_id] = distances_to_destination_excluding(
            graph, destination, elevator_id
        )

    start_state = (start, 0)
    distances: dict[tuple[str, int], int] = {start_state: 0}
    previous: dict[tuple[str, int], tuple[tuple[str, int], Edge]] = {}
    counter = 0
    heap: list[tuple[int, int, str, int]] = [(0, counter, start, 0)]
    goal_state: tuple[str, int] | None = None

    while heap:
        current_cost, _, node, mask = heapq.heappop(heap)
        state = (node, mask)
        if current_cost != distances.get(state):
            continue
        if node == destination:
            goal_state = state
            break

        for edge in graph.outgoing.get(node, []):
            next_mask = mask
            elevator_id = edge.elevator_id

            if elevator_id is not None:
                elevator_index = graph.elevator_index[elevator_id]
                bit = 1 << elevator_index
                if not (mask & bit):
                    fallback_distance = fallback_distances[elevator_id].get(node)
                    if fallback_distance is None:
                        continue
                    # The elevator ride itself is not consumed when outage is found.
                    if current_cost + fallback_distance > budget_minutes:
                        continue
                next_mask = mask | bit

            new_cost = current_cost + edge.minutes
            if new_cost > budget_minutes:
                continue

            next_state = (edge.target, next_mask)
            if new_cost < distances.get(next_state, 10**30):
                distances[next_state] = new_cost
                previous[next_state] = (state, edge)
                counter += 1
                heapq.heappush(
                    heap, (new_cost, counter, edge.target, next_mask)
                )

    if goal_state is None:
        raise NoGuaranteedRoute(
            "无法保证赶上接驳车：虽然存在无停运时可到达的路线，但没有任何路线能在"
            "至多一台电梯停运时始终用剩余时间绕行赶到接驳点；本次不开始引路。",
            reason_code="NO_GUARANTEED_ROUTE",
            fastest_minutes=fastest_minutes,
            fastest_edges=fastest_edges,
        )

    reversed_edges: list[Edge] = []
    state = goal_state
    while state != start_state:
        previous_state, edge = previous[state]
        reversed_edges.append(edge)
        state = previous_state
    reversed_edges.reverse()

    total_minutes = distances[goal_state]
    contingencies = build_contingencies(
        graph, destination, reversed_edges, budget_minutes
    )

    return PlannedRoute(
        edges=reversed_edges,
        total_minutes=total_minutes,
        contingencies=contingencies,
    )
