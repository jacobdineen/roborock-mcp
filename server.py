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
