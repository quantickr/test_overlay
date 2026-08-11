"""
Модуль 2 (Суфлёр / Оверлей) — Telethon / user-сессия.

Прозрачное окно без рамок поверх всех окон. В фоне слушает целевой чат
(TARGET_CHAT) через Telethon и мгновенно выводит новый входящий текст крупным
шрифтом. Текст плавно гаснет после таймаута.

Сквозной клик мышью (click-through) реализован для Windows через WinAPI:
к окну добавляются стили WS_EX_LAYERED | WS_EX_TRANSPARENT, благодаря чему
клики проходят «сквозь» оверлей к приложениям под ним.

Архитектура потоков:
  - Telethon работает в фоновом потоке со своим asyncio-loop и складывает
    входящий текст в потокобезопасную очередь.
  - Tkinter (главный поток) периодически опрашивает очередь через after().

Первый запуск: Telethon спросит номер телефона и код подтверждения, создаст
файл сессии SESSION_OVERLAY (.session).

Управление:
  Escape — выход. (Перетаскивание отключено: окно кликопрозрачное.)
"""

import asyncio
import queue
import sys
import threading
import tkinter as tk

from telethon import TelegramClient, events

import config


# Через сколько мс после последнего сообщения начать плавно гасить текст.
FADE_AFTER_MS = 6000
FADE_STEP_MS = 60
FADE_STEP = 0.05

# Внешний вид.
FONT = ("Arial", 40, "bold")
TEXT_COLOR = "#FFFFFF"
# Цвет chroma key, который делается прозрачным (Windows).
TRANSPARENT_COLOR = "#010101"
WINDOW_ALPHA = 1.0


class TelethonListener(threading.Thread):
    """Фоновый поток: слушает целевой чат и кладёт тексты в очередь."""

    def __init__(self, out_queue: "queue.Queue[str]"):
        super().__init__(daemon=True)
        self.out_queue = out_queue
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self) -> None:
        client = TelegramClient(config.SESSION_OVERLAY, config.API_ID,
                                config.API_HASH)

        @client.on(events.NewMessage(chats=config.TARGET_CHAT))
        async def handler(event):  # noqa: ANN001
            text = event.message.message
            if text:
                self.out_queue.put(text)

        await client.start()
        me = await client.get_me()
        print(f"[Суфлёр] Вход как: {me.first_name} (id={me.id})")
        print(f"[Суфлёр] Слушаю чат: {config.TARGET_CHAT}")
        self._ready.set()
        await client.run_until_disconnected()

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)


def enable_click_through(root: tk.Tk) -> None:
    """Windows: делает окно кликопрозрачным (клики проходят насквозь)."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        from ctypes import wintypes

        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())

        user32 = ctypes.windll.user32
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetWindowLongW.argtypes = [
            wintypes.HWND, ctypes.c_int, ctypes.c_long
        ]

        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    except Exception as exc:  # noqa: BLE001
        print(f"[Суфлёр] Не удалось включить сквозной клик: {exc}",
              file=sys.stderr)


class Overlay:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.overrideredirect(True)          # без рамок
        self.root.attributes("-topmost", True)    # поверх всех окон
        self.root.attributes("-alpha", WINDOW_ALPHA)

        try:
            self.root.attributes("-transparentcolor", TRANSPARENT_COLOR)
        except tk.TclError:
            pass

        self.root.configure(bg=TRANSPARENT_COLOR)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        width, height = sw, 220
        x, y = 0, sh - height - 80
        self.root.geometry(f"{width}x{height}+{x}+{y}")

        self.label = tk.Label(
            self.root,
            text="",
            font=FONT,
            fg=TEXT_COLOR,
            bg=TRANSPARENT_COLOR,
            wraplength=width - 80,
            justify="center",
        )
        self.label.pack(expand=True, fill="both", padx=40, pady=20)

        self._fade_after_id = None
        self._fade_step_id = None
        self._current_alpha = WINDOW_ALPHA

        # Escape для выхода. Клавиатура ловится, даже если окно кликопрозрачное,
        # пока оно в фокусе; для надёжного выхода можно закрыть из консоли Ctrl+C.
        self.root.bind("<Escape>", lambda e: self.stop())

        self.queue: "queue.Queue[str]" = queue.Queue()
        self.listener = TelethonListener(self.queue)

    def _show_text(self, text: str) -> None:
        if self._fade_after_id is not None:
            self.root.after_cancel(self._fade_after_id)
        if self._fade_step_id is not None:
            self.root.after_cancel(self._fade_step_id)
            self._fade_step_id = None

        self._current_alpha = WINDOW_ALPHA
        self.root.attributes("-alpha", self._current_alpha)
        self.label.config(text=text)

        self._fade_after_id = self.root.after(FADE_AFTER_MS, self._start_fade)

    def _start_fade(self) -> None:
        self._current_alpha -= FADE_STEP
        if self._current_alpha <= 0:
            self.label.config(text="")
            self.root.attributes("-alpha", WINDOW_ALPHA)
            self._current_alpha = WINDOW_ALPHA
            self._fade_step_id = None
            return
        self.root.attributes("-alpha", self._current_alpha)
        self._fade_step_id = self.root.after(FADE_STEP_MS, self._start_fade)

    def _poll_queue(self) -> None:
        last_text = None
        try:
            while True:
                last_text = self.queue.get_nowait()
        except queue.Empty:
            pass
        if last_text is not None:
            self._show_text(last_text)
        self.root.after(100, self._poll_queue)

    def run(self) -> None:
        if not config.is_configured():
            print("Заполните API_ID / API_HASH в config.py "
                  "(или переменные TG_API_ID / TG_API_HASH).", file=sys.stderr)
            sys.exit(1)

        # Включаем сквозной клик после создания окна.
        enable_click_through(self.root)

        self.listener.start()
        self.root.after(100, self._poll_queue)
        self.root.mainloop()

    def stop(self) -> None:
        self.listener.stop()
        self.root.destroy()


if __name__ == "__main__":
    Overlay().run()
