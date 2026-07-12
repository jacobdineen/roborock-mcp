import asyncio, sys
from roborock.web_api import RoborockApiClient

async def main():
    client = RoborockApiClient(sys.argv[1])
    await client.request_code_v4()
    print("verification code sent to", sys.argv[1])

asyncio.run(main())
