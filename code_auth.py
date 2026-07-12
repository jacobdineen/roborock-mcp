"""Complete login with the emailed verification code; caches session token."""
import asyncio, json, os, sys
from roborock.web_api import RoborockApiClient

EMAIL, CODE = sys.argv[1], sys.argv[2]
CACHE = os.path.expanduser("~/.roborock-mcp/credentials.json")

async def main():
    client = RoborockApiClient(EMAIL)
    user_data = await client.code_login_v4(CODE)
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w") as f:
        json.dump({"email": EMAIL, "user_data": user_data.as_dict(),
                   "base_url": await client.base_url}, f)
    os.chmod(CACHE, 0o600)
    home = await client.get_home_data_v3(user_data)
    for d in home.devices + home.received_devices:
        prod = next((p for p in home.products if p.id == d.product_id), None)
        print(f"device: {d.name} | model: {prod.model if prod else '?'} | online: {d.online}")
    print("token cached ->", CACHE)

asyncio.run(main())
