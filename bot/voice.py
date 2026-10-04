"""Speech-to-text via the user's own OpenAI-compatible speech server (speaches).

The audio of a voice note travels only to that LAN server, configured by SPEECH_API_URL.
Voice is optional: without SPEECH_API_URL the bot runs fine and voice notes get a
"not set up" reply instead of an error.
"""

import os

from openai import OpenAI

STT_MODEL_DEFAULT = "Systran/faster-whisper-tiny"

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
