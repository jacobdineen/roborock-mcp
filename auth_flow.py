"""One-time Roborock auth: request an email code and log in, in one process.

Roborock binds the emailed code to the requesting client's device ID, which
python-roborock randomizes per RoborockApiClient instance — so the request and
the login must share one instance. This script requests the code, then waits
for it (typed interactively, or written to ~/.roborock-mcp/code.txt when
running non-interactively).

Usage: python auth_flow.py you@example.com
"""
import asyncio
import json
import os
import sys
import time

from roborock.web_api import RoborockApiClient

CACHE = os.path.expanduser("~/.roborock-mcp/credentials.json")
CODE_FILE = os.path.expanduser("~/.roborock-mcp/code.txt")


async def get_code() -> str:
    if sys.stdin.isatty():
        return input("verification code: ").strip()
    print("code requested; waiting for", CODE_FILE, flush=True)
    deadline = time.time() + 900
    while time.time() < deadline:
        if os.path.exists(CODE_FILE):
            code = open(CODE_FILE).read().strip()
            if code:
                os.remove(CODE_FILE)
                return code
        await asyncio.sleep(2)
    raise SystemExit("timed out waiting for code")


async def main():
    email = sys.argv[1]
    client = RoborockApiClient(email)
    await client.request_code()
    code = await get_code()
    print("got code, logging in...", flush=True)
    user_data = await client.code_login(code)
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w") as f:
        json.dump(
            {"email": email, "user_data": user_data.as_dict(), "base_url": await client.base_url},
            f,
        )
    os.chmod(CACHE, 0o600)
    home = await client.get_home_data_v3(user_data)
    for d in home.devices + home.received_devices:
        prod = next((p for p in home.products if p.id == d.product_id), None)
        print(f"device: {d.name} | model: {prod.model if prod else '?'} | online: {d.online}")
    print("token cached ->", CACHE)


asyncio.run(main())
