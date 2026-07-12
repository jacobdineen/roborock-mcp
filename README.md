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
| `pause` / `resume` / `stop` (robot) | Job control |
| `return_to_dock(robot)` | Send it home |
| `locate(robot)` | Play the "I'm here" sound |
| `get_consumables(robot)` | Brush/filter/sensor life remaining |
| `send_raw_command(robot, command, params_json)` | Any `RoborockCommand`, escape hatch |

## Setup

```bash
uv venv && uv pip install -e .
```

### Authenticate (one time)

Roborock accounts with two-step verification need the email-code flow:

```bash
.venv/bin/python request_code.py you@example.com   # sends code to your email
.venv/bin/python code_auth.py you@example.com 123456
```

Accounts without two-step can use the password flow instead:

```bash
RR_PASS='...' .venv/bin/python auth_once.py you@example.com
```

Either path caches a session token at `~/.roborock-mcp/credentials.json`
(mode 600). No password is ever stored.

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
