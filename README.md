# roborock-mcp

MCP server for controlling Roborock vacuums from Claude Code (or any MCP client).
Built on [python-roborock](https://github.com/Python-roborock/python-roborock) — the
same library behind Home Assistant's Roborock integration. All robots on the
account are first-class: every tool takes a `robot` argument matched by
(partial, case-insensitive) name.

## Tools

| Tool | What it does |
|---|---|
| `list_robots` | All vacuums with state, battery, errors |
| `get_status(robot)` | Detailed live status for one robot |
| `list_rooms(robot)` | Segment id → room name for the robot's map |
| `start_cleaning(robot)` | Full clean |
| `clean_rooms(robot, rooms, repeat)` | Clean specific rooms by name or id |
| `clean_house(rooms, exclude, repeat, dry_run)` | Split a whole-house clean across all robots, area-balanced (`dry_run` returns the plan only) |
| `pause` / `resume` / `stop` (robot) | Job control |
| `return_to_dock(robot)` | Send it home |
| `fleet_status()` | Live status of every robot in one call |
| `pause_all` / `resume_all` / `dock_all` | Fan a job command to every robot at once |
| `locate(robot)` | Play the "I'm here" sound |
| `get_consumables(robot)` | Brush/filter/sensor life remaining |
| `send_raw_command(robot, command, params_json)` | Any `RoborockCommand`, escape hatch |

## Setup

```bash
uv venv && uv pip install -e .
```

### Authenticate (one time)

```bash
.venv/bin/python auth_flow.py you@example.com
```

This emails you a verification code and waits for it (type it in, or write it
to `~/.roborock-mcp/code.txt` when running non-interactively). Request and
login must happen in one process — Roborock binds the code to the requesting
client's randomized device ID, so requesting and logging in from separate
processes fails with "invalid code". If login fails with a user-agreement
error (3009), open the Roborock app, accept the ToS popup, and rerun.

The session token is cached at `~/.roborock-mcp/credentials.json` (mode 600).
No password is ever stored.

### Register with Claude Code

```bash
claude mcp add roborock -s user -- /path/to/roborock-mcp/.venv/bin/python /path/to/roborock-mcp/server.py
```

Then just ask: *"have the upstairs robot clean the kitchen and the office, two passes."*

## Notes

- Connection prefers local TCP to the robot, falling back to Roborock's cloud
  MQTT — works even when you're away from home.
- Room names come from the map in the Roborock app; rename rooms there and
  `list_rooms` picks them up.
- `clean_house` splits the house across robots by canonical room name: rooms only
  one robot has go to that robot, rooms both have are greedily balanced by
  estimated floor area so the robots finish together. Area estimates come from a
  static per-segment bbox table (`~/.roborock-mcp/rhonda_bboxes.json`); rooms with
  no estimate fall back to the median room size. Run with `dry_run=True` to see
  the partition before dispatching.
