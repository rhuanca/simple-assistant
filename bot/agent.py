import contextvars
import os
from typing import Annotated

from cachetools import TTLCache
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langsmith import traceable
from pydantic import Field

from bot import storage

SYSTEM_PROMPT = """\
You are a grocery list assistant in a Telegram chat. You help users manage their shopping lists.
The user may write in English or Spanish — always reply in the same language they used.

You have tools to add items, remove items, show a list, and clear a list.
Always use the "groceries" list for all operations.

When the user asks to add or remove items, extract clean item names without articles or filler words.

When a [Recently viewed list] block appears before the user's message, every row has the
form "N. text (id=X)". Use it to resolve references:
  - References by number, position, or ordinal ("drop #7", "el séptimo", "the third one")
    → look up the row by N.
  - References by name ("remove the cheese", "quita el jamón") → look up the row by text.
In either case, call remove_items_by_id passing parallel lists: `ids` and the matching
`texts`. If the tool reports the cache is stale, call show_list and retry from the
fresh block.

If no [Recently viewed list] block is present, call show_list first to fetch the current
state. Only fall back to remove_items (name-based) when you cannot identify a row by id.

When you cannot understand the request, ask for clarification in the user's language.
Keep responses short and friendly.
"""

_view_cache: TTLCache = TTLCache(maxsize=1000, ttl=30 * 60)
_current_user_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_user_id", default=None
)


def _cache_view(user_id: int, list_name: str, raw_items: list[dict]) -> None:
    _view_cache[user_id] = {
        "list_name": list_name,
        "items": [
            {"number": i, "id": item["id"], "text": item["item_text"]}
            for i, item in enumerate(raw_items, 1)
        ],
    }


def _get_cached_view(user_id: int) -> dict | None:
    return _view_cache.get(user_id)


def _refresh_user_cache() -> None:
    user_id = _current_user_id.get()
    if user_id is None:
        return
    entry = _view_cache.get(user_id)
    if entry is None:
        return
    _cache_view(user_id, entry["list_name"], storage.get_items(entry["list_name"]))


def _format_view_block(view: dict) -> str:
    if not view["items"]:
        return f"[Recently viewed {view['list_name']} list: (empty)]"
    lines = [f"  {it['number']}. {it['text']} (id={it['id']})" for it in view["items"]]
    return f"[Recently viewed {view['list_name']} list:\n" + "\n".join(lines) + "]"


_LIST_NAME_ARG = Annotated[str, Field(description="Which list — 'groceries' or 'house'.")]


@tool
def add_items(
    items: Annotated[list[str], Field(description="Item names to add.")],
    list_name: _LIST_NAME_ARG = "groceries",
    added_by: Annotated[str, Field(description="Name of the user adding the items.")] = "",
) -> str:
    """Add one or more items to a shopping list."""
    for item in items:
        storage.add_item(item, list_name=list_name, added_by=added_by)
    _refresh_user_cache()
    return f"Added to {list_name}: {', '.join(items)}"


@tool
def remove_items(
    items: Annotated[list[str], Field(description="Item names to remove (use only when no [Recently viewed list] block is available).")],
    list_name: _LIST_NAME_ARG = "groceries",
) -> str:
    """Remove one or more items from a shopping list by name."""
    removed = []
    not_found = []
    for item in items:
        if storage.remove_item(item, list_name=list_name):
            removed.append(item)
        else:
            not_found.append(item)
    _refresh_user_cache()
    parts = []
    if removed:
        parts.append(f"Removed: {', '.join(removed)}")
    if not_found:
        parts.append(f"Not found: {', '.join(not_found)}")
    return " | ".join(parts) if parts else "Nothing to remove."


@tool
def remove_items_by_id(
    ids: Annotated[list[int], Field(description="Database ids from the [Recently viewed list] block.")],
    texts: Annotated[list[str], Field(description="Texts matching `ids` 1-to-1, for verification.")],
) -> str:
    """Remove items by database id. Use this whenever the [Recently viewed list]
    block contains the items the user is referring to.

    Pass parallel lists from the block. Each row's current text is verified
    against `texts` before deleting; mismatches are reported as stale (call
    show_list and retry with fresh ids).
    """
    removed, not_found, stale = [], [], []

    for item_id, expected in zip(ids, texts):
        current = storage.get_item_by_id(item_id)
        if current is None:
            not_found.append(item_id)
        elif current["item_text"].lower() != expected.lower():
            stale.append(f"id={item_id}: expected '{expected}', got '{current['item_text']}'")
        else:
            storage.remove_item_by_id(item_id)
            removed.append(current["item_text"])

    _refresh_user_cache()

    report = []
    if removed:
        report.append(f"Removed: {', '.join(removed)}")
    if not_found:
        report.append(f"Not found (ids: {', '.join(str(i) for i in not_found)})")
    if stale:
        report.append("Cache stale: " + "; ".join(stale))
    return " | ".join(report) or "Nothing to remove."


@tool
def show_list(list_name: _LIST_NAME_ARG = "groceries") -> str:
    """Show all items currently on a shopping list."""
    items = storage.get_items(list_name)
    user_id = _current_user_id.get()
    if user_id is not None:
        _cache_view(user_id, list_name, items)
    if not items:
        return f"The {list_name} list is empty."
    lines = [f"{list_name} ({len(items)} items):"]
    for i, item in enumerate(items, 1):
        lines.append(f"  {i}. {item['item_text']}")
    return "\n".join(lines)


@tool
def clear_list(list_name: _LIST_NAME_ARG = "groceries") -> str:
    """Clear all items from a shopping list."""
    count = storage.clear_list(list_name)
    _refresh_user_cache()
    return f"Cleared {count} items from {list_name}."


class AgentError(Exception):
    def __init__(self, user_message: str, admin_detail: str):
        super().__init__(admin_detail)
        self.user_message = user_message
        self.admin_detail = admin_detail


_tools = [add_items, remove_items, remove_items_by_id, show_list, clear_list]
_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        model = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=os.getenv("GEMINI_API_KEY"),
            temperature=0,
        )
        _agent = create_agent(model, _tools, system_prompt=SYSTEM_PROMPT)
    return _agent


@traceable(run_type="prompt", name="build_prompt")
def _build_prompt(text: str, user: str, user_id: int | None) -> str:
    parts = []
    if user_id is not None:
        view = _get_cached_view(user_id)
        if view is not None:
            parts.append(_format_view_block(view))
    if user:
        parts.append(f"[from {user}]")
    parts.append(text)
    return "\n".join(parts)


@traceable(name="extract_response")
def _extract_text(result: dict) -> str:
    content = result["messages"][-1].content
    if isinstance(content, list):
        text = "".join(block["text"] for block in content if block.get("text"))
    else:
        text = content or ""
    return text.strip() or "OK"


@traceable(name="grocery_bot.run", tags=["telegram"])
async def run(text: str, user: str = "", user_id: int | None = None) -> str:
    agent = _get_agent()
    message = _build_prompt(text, user, user_id)

    token = _current_user_id.set(user_id)
    try:
        try:
            result = await agent.ainvoke({"messages": [HumanMessage(content=message)]})
        except Exception as exc:
            error = str(exc)
            if "PERMISSION_DENIED" in error or "403" in error:
                raise AgentError(
                    user_message=(
                        "I cannot access Gemini right now (permission denied). "
                        "Please update your Google AI project/API key, then try again."
                    ),
                    admin_detail=f"Gemini permission denied: {error}",
                ) from exc
            raise AgentError(
                user_message="I hit an unexpected model error. Please try again in a moment.",
                admin_detail=f"Model error: {error}",
            ) from exc
    finally:
        _current_user_id.reset(token)

    return _extract_text(result)
