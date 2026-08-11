"""
Единое приложение (одна машина, один запуск).

Что делает:
  1. Захватывает СИСТЕМНЫЙ звук (то, что играет в динамиках) через WASAPI
     loopback (библиотека soundcard).
  2. Распознаёт речь через SpeechRecognition (Google Web Speech API).
  3. Отправляет распознанный текст в общую группу Telegram (TARGET_CHAT).
  4. Показывает прозрачный суфлёр поверх всех окон, выводя ТОЛЬКО сообщения
     помощника — то есть все сообщения группы, КРОМЕ отправленных с этого же
     аккаунта (эхо собственного распознавания скрывается).

Архитектура потоков:
  - Telethon работает в фоновом потоке со своим asyncio-loop.
  - Захват звука + распознавание — в отдельном фоновом потоке.
  - Tkinter (суфлёр) — в главном потоке, опрашивает очередь через after().

Запуск (Windows):
  python app.py

Первый запуск попросит телефон и код подтверждения Telegram.
Выход: Escape (в фокусе окна) или Ctrl+C в консоли.
"""

import asyncio
import queue
import sys
import threading
import tkinter as tk

import numpy as np
import speech_recognition as sr
from telethon import TelegramClient, events

import config


# --- Настройки распознавания ---
LANGUAGE = "ru-RU"
SAMPLE_RATE = 16000          # частота для распознавания

# --- Настройки VAD (нарезка речи по паузам) ---
# Звук читается короткими кадрами; по громкости каждого кадра определяем,
# речь это или тишина. Фраза копится, пока идёт речь, и отправляется на
# распознавание после паузы SILENCE_HANG_MS.
VAD_FRAME_MS = 30            # длительность одного кадра анализа (мс)
# Порог громкости (RMS, диапазон 0..1). Кадр громче порога считается речью.
# Поднимите, если ловится фоновый шум; снизьте, если теряется тихая речь.
VAD_RMS_THRESHOLD = 0.012
# Сколько мс тишины завершает фразу и отправляет её на распознавание.
SILENCE_HANG_MS = 700
# Минимальная длина фразы (мс) — короче считаем случайным шумом и пропускаем.
MIN_PHRASE_MS = 350
# Защита от бесконечной фразы: принудительно режем после этого лимита.
MAX_PHRASE_MS = 15000

# --- Внешний вид суфлёра ---
FADE_AFTER_MS = 6000
FADE_STEP_MS = 60
FADE_STEP = 0.05
FONT = ("Arial", 40, "bold")
TEXT_COLOR = "#FFFFFF"
TRANSPARENT_COLOR = "#010101"
WINDOW_ALPHA = 1.0


class TelegramWorker(threading.Thread):
    """Фоновый поток с Telethon: приём сообщений помощника + отправка текста."""

    def __init__(self, incoming_queue: "queue.Queue[str]"):
        super().__init__(daemon=True)
        self.incoming_queue = incoming_queue      # тексты помощника -> в суфлёр
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: TelegramClient | None = None
        self._ready = threading.Event()
        self.connected = False   # True, если вход в Telegram удался

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self) -> None:
        proxy = getattr(config, "PROXY", None)
        self._client = TelegramClient(config.SESSION_OVERLAY, config.API_ID,
                                      config.API_HASH, proxy=proxy)

        @self._client.on(events.NewMessage(chats=config.TARGET_CHAT))
        async def handler(event):  # noqa: ANN001
            # event.out == True для сообщений, отправленных С ЭТОГО аккаунта
            # (эхо нашего собственного sender'а). Их не показываем.
            if event.out:
                return
            text = event.message.message
            if text:
                self.incoming_queue.put(text)

        try:
            await self._client.start()
        except (ConnectionError, OSError, asyncio.IncompleteReadError) as exc:
            print("\n[TG] НЕ УДАЛОСЬ ПОДКЛЮЧИТЬСЯ К TELEGRAM.", file=sys.stderr)
            print(f"[TG] Причина: {exc}", file=sys.stderr)
            print("[TG] Скорее всего Telegram заблокирован провайдером.\n"
                  "     -> Включите VPN на весь ПК (тот, с которым работает "
                  "обычный Telegram) ДО запуска app.py.\n"
                  "     -> Или задайте SOCKS5-прокси в config.py (PROXY).",
                  file=sys.stderr)
            self._ready.set()   # разблокируем ожидание, чтобы приложение вышло
            return

        me = await self._client.get_me()
        print(f"[TG] Вход как: {me.first_name} (id={me.id})")
        print(f"[TG] Чат: {config.TARGET_CHAT}")
        self.connected = True
        self._ready.set()
        await self._client.run_until_disconnected()

    def wait_ready(self, timeout: float = 60) -> bool:
        return self._ready.wait(timeout)

    def send_text(self, text: str) -> None:
        """Потокобезопасная отправка текста в группу."""
        if self._loop is None or self._client is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._client.send_message(config.TARGET_CHAT, text),
            self._loop,
        )

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)


class SystemAudioRecognizer(threading.Thread):
    """Фоновый поток: захват системного звука (loopback) + распознавание."""

    def __init__(self, tg: TelegramWorker):
        super().__init__(daemon=True)
        self.tg = tg
        self._stop = threading.Event()
        self._recognizer = sr.Recognizer()

    def stop(self) -> None:
        self._stop.set()

    def _get_loopback_mic(self):
        """Возвращает loopback-устройство динамиков по умолчанию (Windows)."""
        import soundcard as sc

        speaker = sc.default_speaker()
        # Ищем микрофон-loopback, соответствующий динамику по умолчанию.
        for mic in sc.all_microphones(include_loopback=True):
            if getattr(mic, "isloopback", False) and speaker.name in mic.name:
                return mic
        # Фолбэк: первый доступный loopback.
        loopbacks = [m for m in sc.all_microphones(include_loopback=True)
                     if getattr(m, "isloopback", False)]
        if loopbacks:
            return loopbacks[0]
        return None

    def run(self) -> None:
        try:
            import soundcard as sc  # noqa: F401
        except ImportError:
            print("[Audio] Не установлена библиотека soundcard "
                  "(pip install soundcard).", file=sys.stderr)
            return

        mic = self._get_loopback_mic()
        if mic is None:
            print("[Audio] Не найдено loopback-устройство для захвата "
                  "системного звука.", file=sys.stderr)
            return

        print(f"[Audio] Захват системного звука: {mic.name}")

        # Размер кадра анализа в сэмплах.
        frame_len = int(SAMPLE_RATE * VAD_FRAME_MS / 1000)
        # Сколько подряд тихих кадров завершает фразу.
        hang_frames = int(SILENCE_HANG_MS / VAD_FRAME_MS)
        min_frames = int(MIN_PHRASE_MS / VAD_FRAME_MS)
        max_frames = int(MAX_PHRASE_MS / VAD_FRAME_MS)

        # Состояние VAD.
        speech_frames: list[np.ndarray] = []   # кадры текущей фразы
        silence_run = 0                        # подряд тихих кадров
        in_speech = False

        try:
            with mic.recorder(samplerate=SAMPLE_RATE, channels=1) as rec:
                while not self._stop.is_set():
                    block = rec.record(numframes=frame_len)
                    mono = block[:, 0] if block.ndim > 1 else block

                    rms = float(np.sqrt(np.mean(np.square(mono))))
                    is_speech = rms >= VAD_RMS_THRESHOLD

                    if is_speech:
                        speech_frames.append(mono)
                        silence_run = 0
                        in_speech = True
                        # Аварийная нарезка слишком длинной фразы.
                        if len(speech_frames) >= max_frames:
                            self._flush(speech_frames, min_frames)
                            speech_frames = []
                            in_speech = False
                    elif in_speech:
                        # Пауза внутри речи: продолжаем копить, считаем тишину.
                        speech_frames.append(mono)
                        silence_run += 1
                        if silence_run >= hang_frames:
                            # Достаточно долгая пауза — фраза закончена.
                            self._flush(speech_frames, min_frames)
                            speech_frames = []
                            silence_run = 0
                            in_speech = False
                    # else: тишина вне речи — ничего не копим.
        except Exception as exc:  # noqa: BLE001
            print(f"[Audio] Ошибка захвата: {exc}", file=sys.stderr)

    def _flush(self, frames: list[np.ndarray], min_frames: int) -> None:
        """Собирает накопленную фразу и отправляет на распознавание."""
        if len(frames) < min_frames:
            return  # слишком короткий фрагмент — вероятно случайный шум

        mono = np.concatenate(frames)
        # float32 [-1,1] -> int16 PCM для SpeechRecognition.
        pcm16 = np.clip(mono * 32767, -32768, 32767).astype(np.int16)
        audio = sr.AudioData(pcm16.tobytes(), SAMPLE_RATE, 2)

        try:
            text = self._recognizer.recognize_google(audio, language=LANGUAGE)
        except sr.UnknownValueError:
            return
        except sr.RequestError as exc:
            print(f"[Распознавание] Ошибка Google: {exc}", file=sys.stderr)
            return

        if text:
            print(f"[Распознано] {text}")
            self.tg.send_text(text)


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
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
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
            self.root, text="", font=FONT, fg=TEXT_COLOR,
            bg=TRANSPARENT_COLOR, wraplength=width - 80, justify="center",
        )
        self.label.pack(expand=True, fill="both", padx=40, pady=20)

        self._fade_after_id = None
        self._fade_step_id = None
        self._current_alpha = WINDOW_ALPHA

        self.root.bind("<Escape>", lambda e: self.stop())

        self.incoming: "queue.Queue[str]" = queue.Queue()
        self.tg = TelegramWorker(self.incoming)
        self.audio = SystemAudioRecognizer(self.tg)

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
                last_text = self.incoming.get_nowait()
        except queue.Empty:
            pass
        if last_text is not None:
            self._show_text(last_text)
        self.root.after(100, self._poll_queue)

    def run(self) -> None:
        if not config.is_configured():
            print("Заполните API_ID / API_HASH в config.py.", file=sys.stderr)
            sys.exit(1)

        enable_click_through(self.root)

        # Сначала поднимаем Telegram, ждём готовности, затем захват звука.
        self.tg.start()
        if not self.tg.wait_ready(timeout=120):
            print("[TG] Тайм-аут ожидания входа в Telegram.", file=sys.stderr)
            sys.exit(1)
        if not self.tg.connected:
            # Подробная причина уже напечатана в TelegramWorker._main.
            print("[TG] Запуск отменён: нет связи с Telegram.", file=sys.stderr)
            sys.exit(1)
        self.audio.start()

        self.root.after(100, self._poll_queue)
        self.root.mainloop()

    def stop(self) -> None:
        self.audio.stop()
        self.tg.stop()
        self.root.destroy()


if __name__ == "__main__":
    try:
        Overlay().run()
    except KeyboardInterrupt:
        print("\nЗавершение.")
