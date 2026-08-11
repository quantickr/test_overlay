"""
Общая конфигурация для обоих модулей (Telethon / user-сессия).

Как получить api_id и api_hash:
  1. Откройте https://my.telegram.org -> "API development tools".
  2. Создайте приложение, скопируйте api_id (число) и api_hash (строка).

TARGET_CHAT — куда отправляет отправитель и что слушает суфлёр. Это может быть:
  - "me"                        — «Избранное» (Saved Messages), удобно для теста;
  - "@username"                 — username чата/канала;
  - числовой id                 — например -100XXXXXXXXXX для канала/супергруппы.

Отправитель и суфлёр используют РАЗНЫЕ файлы сессии (SESSION_SENDER /
SESSION_OVERLAY), чтобы не конфликтовать при одновременной работе. При первом
запуске каждого модуля Telethon попросит номер телефона и код подтверждения.

Секреты задаются через переменные окружения TG_API_ID, TG_API_HASH,
TG_TARGET_CHAT, TG_PROXY — либо через файл .env рядом с этим модулем
(см. .env.example). Файл .env не коммитится.
"""

import os

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _load_dotenv(path: str = _ENV_PATH) -> None:
    """Мини-загрузчик .env: строки KEY=VALUE, # — комментарий.
    Не перекрывает уже установленные переменные окружения."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()

API_ID = int(os.environ.get("TG_API_ID", "0").strip() or 0)
API_HASH = os.environ.get("TG_API_HASH", "").strip()


def _get_target_chat():
    raw = os.environ.get("TG_TARGET_CHAT", "").strip()
    if not raw:
        return "me"
    # Числовой id (в т.ч. отрицательный) приводим к int; username оставляем строкой.
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


TARGET_CHAT = _get_target_chat()

# Имена файлов сессии Telethon (создаются автоматически, .session).
SESSION_SENDER = "sender_session"
SESSION_OVERLAY = "overlay_session"

# --- Прокси (опционально) ---
# Если Telegram заблокирован провайдером и системного VPN недостаточно, можно
# задать SOCKS5-прокси через TG_PROXY в формате "socks5:ХОСТ:ПОРТ".
# Обычно достаточно включить системный VPN на весь ПК — тогда переменную
# можно не задавать (PROXY останется None).
def _get_proxy():
    raw = os.environ.get("TG_PROXY", "").strip()
    if not raw:
        return None
    scheme, host, port = raw.split(":")
    return (scheme, host, int(port))


PROXY = _get_proxy()


def is_configured() -> bool:
    """True, если api_id/api_hash заполнены."""
    return API_ID > 0 and bool(API_HASH)
