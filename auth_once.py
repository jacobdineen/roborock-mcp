"""One-time login: exchanges email+password for a session token, caches it."""
import asyncio, json, os, sys
from roborock.web_api import RoborockApiClient

EMAIL = sys.argv[1]
PASSWORD = os.environ["RR_PASS"]
CACHE = os.path.expanduser("~/.roborock-mcp/credentials.json")

async def main():
    client = RoborockApiClient(EMAIL)
    user_data = await client.pass_login(PASSWORD)
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w") as f:
        json.dump({"email": EMAIL, "user_data": user_data.as_dict()}, f)
    os.chmod(CACHE, 0o600)
    home = await client.get_home_data_v3(user_data)
    for d in home.devices + home.received_devices:
        prod = next((p for p in home.products if p.id == d.product_id), None)
        print(f"device: {d.name} | model: {prod.model if prod else '?'} | duid: {d.duid} | online: {d.online}")
    print("token cached ->", CACHE)

asyncio.run(main())
