"""Roborock MCP server — controls all vacuums on the account by name.

Auth: run auth_flow.py first to cache a session token at
~/.roborock-mcp/credentials.json. The server never sees the account password.
"""

import asyncio
import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from roborock import RoborockCommand, UserData
from roborock.devices.device_manager import DeviceManager, UserParams, create_device_manager

import map_response_patch  # noqa: F401  (fixes chunked map responses upstream drops)

CACHE = os.path.expanduser("~/.roborock-mcp/credentials.json")
# Static per-segment bounding boxes (mm) used to estimate room floor area when
# the live map doesn't cheaply expose them. Keyed by Rhonda's segment ids.
AREA_FALLBACK = os.path.expanduser("~/.roborock-mcp/rhonda_bboxes.json")

mcp = FastMCP("roborock")

_manager: DeviceManager | None = None
_manager_lock = asyncio.Lock()


async def _get_manager() -> DeviceManager:
    global _manager
    async with _manager_lock:
        if _manager is None:
            with open(CACHE) as f:
                creds = json.load(f)
            params = UserParams(
                username=creds["email"],
                user_data=UserData.from_dict(creds["user_data"]),
                base_url=creds.get("base_url"),
            )
            _manager = await create_device_manager(params)
            await _manager.discover_devices()
        return _manager


async def _find(robot: str):
    """Resolve a device by (partial, case-insensitive) name or duid."""
    manager = await _get_manager()
    devices = await manager.get_devices()
    vacuums = [d for d in devices if d.v1_properties is not None]
    needle = robot.strip().lower()
    for d in vacuums:
        if d.name.lower() == needle or d.duid == robot:
            return d
    partial = [d for d in vacuums if needle in d.name.lower()]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(f"'{d.name}'" for d in vacuums)
    raise ValueError(f"No unique robot matching '{robot}'. Available: {names}")


def _status_dict(d) -> dict[str, Any]:
    s = d.v1_properties.status
    return {
        "name": d.name,
        "model": d.product.model if d.product else None,
        "connected": d.is_connected,
        "local_connection": d.is_local_connected,
        "state": str(getattr(s, "state_name", None) or getattr(s, "state", None)),
        "battery": s.battery,
        "error": str(s.error_code_name) if getattr(s, "error_code", 0) else None,
        "cleaning_area_m2": (s.clean_area or 0) / 1_000_000 if s.clean_area else None,
        "cleaning_time_min": round(s.clean_time / 60) if s.clean_time else None,
        "fan_power": str(getattr(s, "fan_power_name", None) or getattr(s, "fan_power", None)),
        "water_box_attached": getattr(s, "water_box_status", None),
        "dock_error": str(s.dock_error_status_name) if getattr(s, "dock_error_status", 0) else None,
    }


async def _vacuums() -> list:
    """All vacuum devices on the account (v1 robots)."""
    manager = await _get_manager()
    devices = await manager.get_devices()
    return [d for d in devices if d.v1_properties is not None]


async def _robot_rooms(d) -> dict[str, int]:
    """Live {room name: segment id} for one robot (refreshes the rooms trait)."""
    rt = d.v1_properties.rooms
    await rt.refresh()
    return {r.name: r.segment_id for r in rt.rooms or []}


_AREA_BY_ROOM: dict[str, float] | None = None


async def _area_by_room(vacuums) -> dict[str, float]:
    """Estimated m² per room name, cached in-process.

    Rooms share canonical names across robots, so one area map serves the house.
    Areas come from the static bbox table (mm) keyed off Rhonda's live segment
    ids; area = (x1-x0)*(y1-y0). Best-effort — an empty map just means the
    balancer falls back to a flat per-room estimate.
    """
    global _AREA_BY_ROOM
    if _AREA_BY_ROOM is not None:
        return _AREA_BY_ROOM
    areas: dict[str, float] = {}
    try:
        with open(AREA_FALLBACK) as f:
            bboxes = json.load(f)
        rhonda = next((d for d in vacuums if "rhonda" in d.name.lower()), None)
        if rhonda is not None:
            seg_to_name = {seg: name for name, seg in (await _robot_rooms(rhonda)).items()}
            for seg_str, box in bboxes.items():
                name = seg_to_name.get(int(seg_str))
                if name and len(box) == 4:
                    x0, y0, x1, y1 = box
                    areas[name.lower()] = abs(x1 - x0) * abs(y1 - y0) / 1_000_000
    except Exception:
        pass
    _AREA_BY_ROOM = areas
    return areas


@mcp.tool()
async def list_robots() -> str:
    """List all Roborock vacuums on the account with their current status."""
    manager = await _get_manager()
    devices = await manager.get_devices()
    out = []
    for d in devices:
        if d.v1_properties is None:
            continue
        try:
            await d.v1_properties.status.refresh()
            out.append(_status_dict(d))
        except Exception as e:  # offline device shouldn't hide the rest
            out.append({"name": d.name, "error": f"unreachable: {e}"})
    return json.dumps(out, indent=2, default=str)


@mcp.tool()
async def get_status(robot: str) -> str:
    """Detailed live status for one robot (state, battery, errors, current job)."""
    d = await _find(robot)
    await d.v1_properties.status.refresh()
    return json.dumps(_status_dict(d), indent=2, default=str)


@mcp.tool()
async def list_rooms(robot: str) -> str:
    """List the rooms (segment id -> name) known to a robot's current map."""
    d = await _find(robot)
    rooms = d.v1_properties.rooms
    await rooms.refresh()
    mapping = {str(r.segment_id): r.name for r in rooms.rooms or []}
    return json.dumps(mapping, indent=2)


async def _home_rooms() -> dict[str, str]:
    """Fetch home-level rooms (iot id -> name) from the Roborock cloud."""
    from roborock.web_api import RoborockApiClient

    with open(CACHE) as f:
        creds = json.load(f)
    client = RoborockApiClient(creds["email"], base_url=creds.get("base_url"))
    home = await client.get_home_data_v3(UserData.from_dict(creds["user_data"]))
    return dict(home.rooms_name_map)


@mcp.tool()
async def list_home_rooms() -> str:
    """List the home-level room entities (id -> name) shared by all robots.

    These are the canonical room names; use link_room to attach a robot's map
    segment to one of them.
    """
    return json.dumps(await _home_rooms(), indent=2)


@mcp.tool()
async def link_room(robot: str, segment_id: int, room_name: str) -> str:
    """Link one map segment on a robot to an existing home room (renames it on the map).

    room_name must match a home room from list_home_rooms. NOTE: the robot's other
    segment links are preserved (the underlying command replaces the full mapping,
    so the current mapping is re-sent with one entry changed).
    """
    d = await _find(robot)
    rooms = await _home_rooms()
    by_name = {name.lower(): iot_id for iot_id, name in rooms.items()}
    iot_id = by_name.get(room_name.lower())
    if iot_id is None:
        return f"No home room named '{room_name}'. See list_home_rooms."
    mapping = await d.v1_properties.command.send(RoborockCommand.GET_ROOM_MAPPING)
    payload = []
    seen = False
    for entry in mapping:
        seg, cur_iot = entry[0], entry[1]
        if seg == segment_id:
            payload.append({"miRoomId": iot_id, "robotRoomId": seg})
            seen = True
        elif cur_iot:
            payload.append({"miRoomId": cur_iot, "robotRoomId": seg})
    if not seen:
        return f"Segment {segment_id} not found on {d.name}. Segments: {[e[0] for e in mapping]}"
    await d.v1_properties.command.send(RoborockCommand.NAME_SEGMENT, payload)
    return f"{d.name}: segment {segment_id} -> '{room_name}'"


@mcp.tool()
async def start_cleaning(robot: str) -> str:
    """Start a full clean with the given robot."""
    d = await _find(robot)
    await d.v1_properties.command.send(RoborockCommand.APP_START)
    return f"{d.name}: full clean started"


@mcp.tool()
async def clean_rooms(robot: str, rooms: list[str], repeat: int = 1) -> str:
    """Clean specific rooms. `rooms` accepts room names (from list_rooms) or segment ids.

    repeat: how many passes over each room (1-3).
    """
    d = await _find(robot)
    rt = d.v1_properties.rooms
    await rt.refresh()
    by_name = {r.name.lower(): r.segment_id for r in rt.rooms or []}
    ids = []
    for r in rooms:
        if r.isdigit():
            ids.append(int(r))
        elif r.lower() in by_name:
            ids.append(by_name[r.lower()])
        else:
            partial = [sid for name, sid in by_name.items() if r.lower() in name]
            if len(partial) != 1:
                return f"Unknown room '{r}' on {d.name}. Rooms: {sorted(by_name)}"
            ids.append(partial[0])
    await d.v1_properties.command.send(
        RoborockCommand.APP_SEGMENT_CLEAN,
        [{"segments": ids, "repeat": max(1, min(3, repeat))}],
    )
    return f"{d.name}: cleaning segments {ids} (repeat={repeat})"


@mcp.tool()
async def clean_house(
    rooms: list[str] = None,
    exclude: list[str] = [],
    repeat: int = 1,
    dry_run: bool = False,
) -> str:
    """Partition a whole-house clean across every robot so they finish together.

    Target rooms default to the union of all rooms across all robots (minus
    `exclude`); pass `rooms` to limit the set (canonical names, fuzzy matched).
    Rooms only one robot knows go to that robot; rooms both know are split to
    balance estimated floor area. `repeat` is passes per room (1-3).

    dry_run=True returns just the assignment plan (robot -> rooms, est m²)
    without moving anything. Otherwise each robot is dispatched its share of the
    house concurrently and the plan is returned with per-robot dispatch results.
    """
    vacuums = await _vacuums()
    if not vacuums:
        return "No robots found."

    # Devices aren't hashable, so key everything below by duid.
    by_duid = {d.duid: d for d in vacuums}

    # Live {lowered name: seg} per robot, plus a canonical display form per name.
    robot_lc: dict[str, dict[str, int]] = {}
    display: dict[str, str] = {}
    for uid, d in by_duid.items():
        try:
            mapping = await asyncio.wait_for(_robot_rooms(d), timeout=30)
        except Exception:
            mapping = {}  # unreachable robot just gets an empty share
        robot_lc[uid] = {n.lower(): s for n, s in mapping.items()}
        for name in mapping:
            display.setdefault(name.lower(), name)

    def resolve(names):
        found, unknown = set(), []
        for n in names:
            key = n.strip().lower()
            if key in display:
                found.add(key)
                continue
            hits = [k for k in display if key in k]
            if len(hits) == 1:
                found.add(hits[0])
            else:
                unknown.append(n)
        return found, unknown

    if rooms:
        targets, unknown_requested = resolve(rooms)
    else:
        targets, unknown_requested = set(display), []
    excluded, _ = resolve(exclude)
    targets -= excluded

    areas = await _area_by_room(vacuums)
    default_area = sorted(areas.values())[len(areas) // 2] if areas else 15.0

    def area_of(key: str) -> float:
        return areas.get(key, default_area)

    assign: dict[str, list[str]] = {uid: [] for uid in by_duid}
    totals: dict[str, float] = {uid: 0.0 for uid in by_duid}
    shared: list[str] = []
    unassigned: list[str] = []
    for key in targets:
        havers = [uid for uid in by_duid if key in robot_lc[uid]]
        if not havers:
            unassigned.append(display[key])
        elif len(havers) == 1:
            assign[havers[0]].append(key)
            totals[havers[0]] += area_of(key)
        else:
            shared.append(key)
    # Greedy balance: largest shared rooms first, each to the robot with the
    # smaller committed area so far.
    for key in sorted(shared, key=area_of, reverse=True):
        havers = [uid for uid in by_duid if key in robot_lc[uid]]
        uid = min(havers, key=lambda u: totals[u])
        assign[uid].append(key)
        totals[uid] += area_of(key)

    plan = []
    dispatch_targets = []
    for uid, d in by_duid.items():
        keys = sorted(assign[uid], key=area_of, reverse=True)
        seg_ids = [robot_lc[uid][k] for k in keys]
        plan.append(
            {
                "robot": d.name,
                "rooms": [display[k] for k in keys],
                "segments": seg_ids,
                "est_m2": round(totals[uid], 1),
            }
        )
        if seg_ids:
            dispatch_targets.append((d, seg_ids))

    result: dict[str, Any] = {"plan": plan}
    if unassigned:
        result["unassigned_no_robot_has_room"] = unassigned
    if unknown_requested:
        result["unknown_rooms_ignored"] = unknown_requested

    if dry_run:
        result["dry_run"] = True
        return json.dumps(result, indent=2, default=str)

    passes = max(1, min(3, repeat))

    async def _dispatch(d, seg_ids):
        await asyncio.wait_for(
            d.v1_properties.command.send(
                RoborockCommand.APP_SEGMENT_CLEAN,
                [{"segments": seg_ids, "repeat": passes}],
            ),
            timeout=30,
        )

    outcomes = await asyncio.gather(
        *(_dispatch(d, ids) for d, ids in dispatch_targets),
        return_exceptions=True,
    )
    dispatched = {
        d.name: ("cleaning" if not isinstance(o, Exception) else f"error: {o}")
        for (d, _), o in zip(dispatch_targets, outcomes)
    }
    for entry in plan:
        entry["dispatch"] = dispatched.get(entry["robot"], "idle (no rooms)")
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
async def pause(robot: str) -> str:
    """Pause the robot's current job."""
    d = await _find(robot)
    await d.v1_properties.command.send(RoborockCommand.APP_PAUSE)
    return f"{d.name}: paused"


@mcp.tool()
async def resume(robot: str) -> str:
    """Resume a paused job."""
    d = await _find(robot)
    await d.v1_properties.command.send(RoborockCommand.APP_START)
    return f"{d.name}: resumed"


@mcp.tool()
async def stop(robot: str) -> str:
    """Stop the current job (robot stays where it is)."""
    d = await _find(robot)
    await d.v1_properties.command.send(RoborockCommand.APP_STOP)
    return f"{d.name}: stopped"


@mcp.tool()
async def return_to_dock(robot: str) -> str:
    """Send the robot home to its dock."""
    d = await _find(robot)
    await d.v1_properties.command.send(RoborockCommand.APP_CHARGE)
    return f"{d.name}: returning to dock"


@mcp.tool()
async def locate(robot: str) -> str:
    """Make the robot announce its location (plays a sound)."""
    d = await _find(robot)
    await d.v1_properties.command.send(RoborockCommand.FIND_ME)
    return f"{d.name}: playing locate sound"


@mcp.tool()
async def get_consumables(robot: str) -> str:
    """Remaining life of brushes, filter, sensors."""
    d = await _find(robot)
    c = d.v1_properties.consumables
    await c.refresh()
    return json.dumps(c.as_dict(), indent=2, default=str)


@mcp.tool()
async def fleet_status() -> str:
    """Live status of every robot at once: state, battery, area/time so far, error."""
    vacuums = await _vacuums()

    async def _one(d):
        await asyncio.wait_for(d.v1_properties.status.refresh(), timeout=30)
        s = _status_dict(d)
        return {k: s[k] for k in ("name", "state", "battery", "cleaning_area_m2", "cleaning_time_min", "error")}

    results = await asyncio.gather(*(_one(d) for d in vacuums), return_exceptions=True)
    out = [
        r if not isinstance(r, Exception) else {"name": d.name, "error": f"unreachable: {r}"}
        for d, r in zip(vacuums, results)
    ]
    return json.dumps(out, indent=2, default=str)


async def _fan(command, verb: str) -> str:
    """Send a single-robot command to every robot concurrently; one failure
    doesn't block the others."""
    vacuums = await _vacuums()

    async def _one(d):
        await asyncio.wait_for(d.v1_properties.command.send(command), timeout=30)

    results = await asyncio.gather(*(_one(d) for d in vacuums), return_exceptions=True)
    out = [
        {"robot": d.name, "ok": True}
        if not isinstance(r, Exception)
        else {"robot": d.name, "ok": False, "error": str(r)}
        for d, r in zip(vacuums, results)
    ]
    return json.dumps({"action": verb, "results": out}, indent=2, default=str)


@mcp.tool()
async def pause_all() -> str:
    """Pause the current job on every robot."""
    return await _fan(RoborockCommand.APP_PAUSE, "pause")


@mcp.tool()
async def resume_all() -> str:
    """Resume a paused job on every robot."""
    return await _fan(RoborockCommand.APP_START, "resume")


@mcp.tool()
async def dock_all() -> str:
    """Send every robot home to its dock."""
    return await _fan(RoborockCommand.APP_CHARGE, "dock")


@mcp.tool()
async def send_raw_command(robot: str, command: str, params_json: str = "") -> str:
    """Escape hatch: send any RoborockCommand (e.g. 'set_custom_mode') with optional JSON params."""
    d = await _find(robot)
    params = json.loads(params_json) if params_json else None
    result = await d.v1_properties.command.send(command, params)
    return json.dumps(result, indent=2, default=str)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
