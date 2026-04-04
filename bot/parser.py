import json
import os
from dataclasses import dataclass

from google import genai

SYSTEM_PROMPT = """\
You are a grocery list assistant. Parse the user's message and return JSON with:
- "intent": one of "add", "remove", "show", "clear", or null if unclear
- "items": list of item names (empty list if not applicable)
- "list_name": which list they mean — "groceries" (default) or "house"

Rules:
- The user may write in English or Spanish
- Extract clean item names without articles or filler words
- "casa", "hogar", "home", "house" → list_name "house"
- "super", "mandado", "grocery" or default → list_name "groceries"
- Return ONLY valid JSON, no markdown, no explanation
"""


@dataclass
class ParsedMessage:
    intent: str | None
    items: list[str]
    list_name: str


_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client


async def parse(text: str) -> ParsedMessage:
    client = _get_client()
    response = await client.aio.models.generate_content(
        model="gemini-2.5-flash",
        contents=text,
        config=genai.types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0,
        ),
    )
    try:
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(raw)
        return ParsedMessage(
            intent=data.get("intent"),
            items=data.get("items", []),
            list_name=data.get("list_name", "groceries"),
        )
    except (json.JSONDecodeError, AttributeError):
        return ParsedMessage(intent=None, items=[], list_name="groceries")
