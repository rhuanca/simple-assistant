"""Speech-to-text and text-to-speech via ElevenLabs (Scribe + Flash TTS).

Voice is optional: without ELEVENLABS_API_KEY the bot runs fine — voice notes get a
"not set up" reply and the daily reminder is text only. The API key goes only to
ElevenLabs, the service that issued it; never wire any other credential in here.
"""

import asyncio
import io
import os
import re

from elevenlabs.client import ElevenLabs

STT_MODEL_DEFAULT = "scribe_v2"
TTS_MODEL_DEFAULT = "eleven_flash_v2_5"  # half the credits of multilingual_v2, ~75ms
TTS_VOICE_DEFAULT = "JBFqnCBsd6RMkjVDRZzb"  # "George", a multilingual stock voice

_client = None


def is_configured() -> bool:
    return bool(os.getenv("ELEVENLABS_API_KEY"))


def _get_client() -> ElevenLabs:
    global _client
    if _client is None:
        _client = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
    return _client


def transcribe(audio: bytes, filename: str = "voice.ogg") -> str:
    """Transcribe a Telegram voice note (OGG/Opus). Blocking — call via asyncio.to_thread."""
    result = _get_client().speech_to_text.convert(
        file=io.BytesIO(audio),
        model_id=os.getenv("ELEVENLABS_STT_MODEL", STT_MODEL_DEFAULT),
        # The household speaks Spanish; pinning the language keeps transcriptions honest.
        language_code="es",
    )
    return result.text.strip()


def synthesize(text: str) -> bytes:
    """Render `text` as MP3 speech. Blocking — call via asyncio.to_thread."""
    chunks = _get_client().text_to_speech.convert(
        text=text,
        voice_id=os.getenv("ELEVENLABS_VOICE_ID", TTS_VOICE_DEFAULT),
        model_id=os.getenv("ELEVENLABS_TTS_MODEL", TTS_MODEL_DEFAULT),
        output_format="mp3_44100_128",  # Telegram's send_voice accepts mp3 directly
    )
    return b"".join(chunks)


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
