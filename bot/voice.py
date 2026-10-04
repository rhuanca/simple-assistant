"""Speech-to-text and text-to-speech via the user's own OpenAI-compatible speech server
(speaches).

Audio travels only to that LAN server, configured by SPEECH_API_URL. Voice is optional:
without SPEECH_API_URL the bot runs fine — voice notes get a "not set up" reply and the
daily reminder is text only.
"""

import asyncio
import os
import re

from openai import OpenAI

STT_MODEL_DEFAULT = "Systran/faster-whisper-tiny"
TTS_MODEL_DEFAULT = "speaches-ai/Kokoro-82M-v1.0-ONNX"
TTS_VOICE_DEFAULT = "ef_dora"  # Kokoro's Spanish voices: ef_dora, em_alex, em_santa

_client = None


def is_configured() -> bool:
    return bool(os.getenv("SPEECH_API_URL"))


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        # The speech server is the user's own and takes no key; the literal is deliberate.
        # Never wire a real credential (Telegram token, Gemini key) in here.
        _client = OpenAI(base_url=os.environ["SPEECH_API_URL"], api_key="not-needed")
    return _client


def transcribe(audio: bytes, filename: str = "voice.ogg") -> str:
    """Transcribe a Telegram voice note (OGG/Opus). Blocking — call via asyncio.to_thread."""
    result = _get_client().audio.transcriptions.create(
        model=os.getenv("SPEECH_STT_MODEL", STT_MODEL_DEFAULT),
        file=(filename, audio),
        # The household speaks Spanish; pinning the language helps faster-whisper-tiny a lot.
        language="es",
    )
    return result.text.strip()


def synthesize(text: str) -> bytes:
    """Render `text` as MP3 speech. Blocking — call via asyncio.to_thread."""
    response = _get_client().audio.speech.create(
        model=os.getenv("SPEECH_TTS_MODEL", TTS_MODEL_DEFAULT),
        voice=os.getenv("SPEECH_TTS_VOICE", TTS_VOICE_DEFAULT),
        input=text,
        response_format="mp3",  # Telegram's send_voice accepts mp3 directly
    )
    return response.content


# Emoji and symbol ranges a TTS voice would either skip or read out loud; bot replies
# are full of them (✅, 🛒, 📅 ...).
_UNSPEAKABLE = re.compile(r"[☀-➿️\U0001F000-\U0001FAFF]")


def speakable(text: str) -> str:
    """Strip emoji and tidy the leftover whitespace so a reply reads well aloud."""
    lines = (_UNSPEAKABLE.sub("", line).strip() for line in text.splitlines())
    return "\n".join(lines).strip()


async def send_voice_note(bot, chat_id: int, spoken: str) -> None:
    """Synthesize and send `spoken` as a voice note, best effort: callers have already
    delivered the text, so a TTS or upload failure must never affect it — log and move on."""
    try:
        audio = await asyncio.to_thread(synthesize, spoken)
        await bot.send_voice(chat_id, audio)
    except Exception as exc:
        print(f"Failed to send voice note to {chat_id}: {exc}")
