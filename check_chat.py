"""
Утилита проверки: логинится через Telethon и проверяет, что TARGET_CHAT
действительно доступен. Запусти один раз после настройки config.py.

  python check_chat.py

При первом запуске Telethon спросит телефон и код подтверждения.
"""

import asyncio

from telethon import TelegramClient

import config


async def main() -> None:
    if not config.is_configured():
        print("Заполните API_ID / API_HASH в config.py.")
        return

    proxy = getattr(config, "PROXY", None)
    client = TelegramClient(config.SESSION_SENDER, config.API_ID,
                            config.API_HASH, proxy=proxy)
    await client.start()

    me = await client.get_me()
    print(f"Вход выполнен как: {me.first_name} (id={me.id})\n")

    try:
        entity = await client.get_entity(config.TARGET_CHAT)
        name = getattr(entity, "title", None) or getattr(entity, "first_name", "")
        print("OK — чат найден:")
        print(f"  id:  {entity.id}")
        print(f"  тип: {type(entity).__name__}")
        print(f"  имя: {name}")
    except Exception as exc:  # noqa: BLE001
        print(f"НЕ найден чат по TARGET_CHAT={config.TARGET_CHAT}: {exc}\n")
        print("Список ваших диалогов (возьмите нужный id отсюда):")
        async for dialog in client.iter_dialogs():
            print(f"  {dialog.id:>15}  |  {dialog.name}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
