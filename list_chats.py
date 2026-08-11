import asyncio
from telethon import TelegramClient
import config

async def main():
    client = TelegramClient(config.SESSION_SENDER, config.API_ID, config.API_HASH)
    await client.start()
    async for dialog in client.iter_dialogs():
        print(f"{dialog.id:>15}  |  {dialog.name}")

asyncio.run(main())