"""
Модуль 1 (Отправитель) — Telethon / user-сессия.

Записывает аудио с микрофона короткими фрагментами, распознаёт русскую речь
через SpeechRecognition (Google Web Speech API) и отправляет распознанный текст
в целевой чат (TARGET_CHAT) от имени вашего аккаунта.

Архитектура:
  - Telethon работает в главном потоке в своём event loop (asyncio).
  - Микрофон и распознавание блокирующие, поэтому крутятся в фоновом потоке;
    готовый текст передаётся в loop через run_coroutine_threadsafe.

Первый запуск: Telethon спросит номер телефона и код подтверждения из Telegram,
создаст файл сессии SESSION_SENDER (.session).

Выход: Ctrl+C.
"""

import asyncio
import sys
import threading

import speech_recognition as sr
from telethon import TelegramClient

import config


# Язык распознавания.
LANGUAGE = "ru-RU"

# Максимальная длительность одного фрагмента (сек).
PHRASE_TIME_LIMIT = 8

# Пауза (сек), после которой фраза считается законченной.
PAUSE_THRESHOLD = 0.8


def recognition_worker(
    loop: asyncio.AbstractEventLoop,
    client: TelegramClient,
    stop_event: threading.Event,
) -> None:
    """Фоновый поток: слушает микрофон, распознаёт речь, шлёт текст в loop."""
    recognizer = sr.Recognizer()
    recognizer.pause_threshold = PAUSE_THRESHOLD

    try:
        microphone = sr.Microphone()
    except OSError as exc:
        print(f"Не удалось открыть микрофон: {exc}", file=sys.stderr)
        loop.call_soon_threadsafe(loop.stop)
        return

    print("Калибровка под окружающий шум... (молчите ~1 сек)")
    with microphone as source:
        recognizer.adjust_for_ambient_noise(source, duration=1)
    print("Готово. Говорите. Нажмите Ctrl+C для выхода.\n")

    while not stop_event.is_set():
        try:
            with microphone as source:
                audio = recognizer.listen(
                    source,
                    phrase_time_limit=PHRASE_TIME_LIMIT,
                )
        except OSError as exc:
            print(f"[Микрофон] Ошибка: {exc}", file=sys.stderr)
            continue

        try:
            text = recognizer.recognize_google(audio, language=LANGUAGE)
        except sr.UnknownValueError:
            continue
        except sr.RequestError as exc:
            print(f"[Распознавание] Ошибка сервиса Google: {exc}",
                  file=sys.stderr)
            stop_event.wait(1)
            continue

        if not text:
            continue

        print(f"[Отправлено] {text}")
        # Передаём отправку в event loop Telethon из фонового потока.
        asyncio.run_coroutine_threadsafe(
            client.send_message(config.TARGET_CHAT, text),
            loop,
        )


async def main() -> None:
    if not config.is_configured():
        print("Заполните API_ID / API_HASH в config.py "
              "(или переменные TG_API_ID / TG_API_HASH).", file=sys.stderr)
        sys.exit(1)

    client = TelegramClient(config.SESSION_SENDER, config.API_ID,
                            config.API_HASH)
    await client.start()

    me = await client.get_me()
    print(f"Вход выполнен как: {me.first_name} (id={me.id})")
    print(f"Целевой чат: {config.TARGET_CHAT}")

    loop = asyncio.get_running_loop()
    stop_event = threading.Event()
    worker = threading.Thread(
        target=recognition_worker,
        args=(loop, client, stop_event),
        daemon=True,
    )
    worker.start()

    try:
        # Держим клиент живым, пока идёт распознавание.
        await client.run_until_disconnected()
    finally:
        stop_event.set()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nЗавершение.")
