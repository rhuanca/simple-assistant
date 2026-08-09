import contextvars
import os
import re
from typing import Annotated, Literal

from cachetools import TTLCache
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langsmith import traceable
from pydantic import Field

from bot import storage

SYSTEM_PROMPT = """\
You are a grocery list assistant in a Telegram chat. You help users manage their shopping lists.
The user may write in English or Spanish — always reply in the same language they used.

Each person has two lists, selected by the `scope` argument of every tool:
  - "personal" — the sender's OWN private list. This is the DEFAULT: use it whenever the
    user does not clearly mean the shared list.
  - "common" — the shared household list. Use it ONLY when the user explicitly refers to the
    shared/house/common list (e.g. "the common list", "the house list", "la lista común",
    "la lista de la casa", "para la casa").
Always pass the `scope` explicitly on every tool call.

You have tools to add items, remove items, show a list, and clear a list.

When the user asks to add or remove items, extract clean item names without articles or filler words.

A [Recently viewed list] block may appear before the user's message. It is internal scratch
data: never quote it, never repeat an id back to the user, and never treat it as the answer
to a request to see a list — to show a list, always call show_list and reproduce its output.
Every row has the form "N. text (id=X)". Use it only to resolve references:
  - References by number, position, or ordinal ("drop #7", "el séptimo", "the third one")
    → look up the row by N.
  - References by name ("remove the cheese", "quita el jamón") → look up the row by text.
In either case, call remove_items_by_id passing parallel lists: `ids` and the matching
`texts`. If the tool reports the cache is stale, call show_list and retry from the
fresh block.

If you need ids and no [Recently viewed list] block is present, call show_list first to
fetch the current state. Only fall back to remove_items (name-based) when you cannot
identify a row by id.

When you cannot understand the request, ask for clarification in the user's language.

Never reply with a bare acknowledgement such as "OK", "Done" or "Listo". Every reply must
tell the user what actually happened.
  - After show_list: reproduce the tool's output, keeping every item and its number exactly
    as given. You may translate only the header line into the user's language.
  - After add, remove or clear: say what changed and on which list.
  - If a list is empty, say so plainly — never answer with nothing.
Keep replies short and friendly, but never empty.
"""

_view_cache: TTLCache = TTLCache(maxsize=1000, ttl=30 * 60)
_current_user_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_user_id", default=None
)


def _owner_for_scope(scope: str) -> int | None:
    """Resolve the storage owner for a tool's scope. 'common' -> None (shared list);
    'personal' -> the acting user's id from the contextvar."""
    if scope == "common":
        return None
    return _current_user_id.get()


def _cache_view(user_id: int, scope: str, raw_items: list[dict]) -> None:
    _view_cache[user_id] = {
        "scope": scope,
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
    scope = entry["scope"]
    _cache_view(user_id, scope, storage.get_items(_owner_for_scope(scope)))


def _format_view_block(view: dict) -> str:
    if not view["items"]:
        return f"[Recently viewed {view['scope']} list: (empty)]"
    lines = [f"  {it['number']}. {it['text']} (id={it['id']})" for it in view["items"]]
    return f"[Recently viewed {view['scope']} list:\n" + "\n".join(lines) + "]"


# Tool return strings are shown to the user verbatim whenever the model ends its turn
# without any text of its own, so they are written as user-facing copy.
_LIST_TITLES = {"personal": "🛒 My list", "common": "🏠 Common list"}
_LIST_TARGETS = {"personal": "your list", "common": "the common list"}


def _render_list(title: str, items: list[dict]) -> str:
    if not items:
        return f"{title} is empty."
    plural = "" if len(items) == 1 else "s"
    lines = [f"{title} — {len(items)} item{plural}"]
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. {item['item_text']}")
    return "\n".join(lines)


_SCOPE_ARG = Annotated[
    Literal["personal", "common"],
    Field(
        description="Which list — 'personal' (the sender's own list, the default) or "
        "'common' (the shared household list). Use 'common' only when the user explicitly "
        "means the shared/house list."
    ),
]


@tool
def add_items(
    items: Annotated[list[str], Field(description="Item names to add.")],
    scope: _SCOPE_ARG = "personal",
    added_by: Annotated[str, Field(description="Name of the user adding the items.")] = "",
) -> str:
    """Add one or more items to a shopping list."""
    owner = _owner_for_scope(scope)
    for item in items:
        storage.add_item(item, owner_user_id=owner, added_by=added_by)
    _refresh_user_cache()
    return f"✅ Added to {_LIST_TARGETS[scope]}: {', '.join(items)}"


@tool
def remove_items(
    items: Annotated[list[str], Field(description="Item names to remove (use only when no [Recently viewed list] block is available).")],
    scope: _SCOPE_ARG = "personal",
) -> str:
    """Remove one or more items from a shopping list by name."""
    owner = _owner_for_scope(scope)
    removed = []
    not_found = []
    for item in items:
        if storage.remove_item(item, owner_user_id=owner):
            removed.append(item)
        else:
            not_found.append(item)
    _refresh_user_cache()
    parts = []
    if removed:
        parts.append(f"✅ Removed from {_LIST_TARGETS[scope]}: {', '.join(removed)}")
    if not_found:
        parts.append(f"⚠️ Not found: {', '.join(not_found)}")
    return "\n".join(parts) if parts else "Nothing to remove."


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
    # Ids are global. Scoping every lookup and delete to the acting user is what stops a
    # guessed or stale id from reaching someone else's personal list.
    acting_user_id = _current_user_id.get()

    for item_id, expected in zip(ids, texts):
        current = storage.get_item_by_id(item_id, acting_user_id)
        if current is None:
            not_found.append(item_id)
        elif current["item_text"].lower() != expected.lower():
            stale.append(f"id={item_id}: expected '{expected}', got '{current['item_text']}'")
        elif storage.remove_item_by_id(item_id, acting_user_id):
            removed.append(current["item_text"])
        else:
            not_found.append(item_id)

    _refresh_user_cache()

    report = []
    if removed:
        report.append(f"✅ Removed: {', '.join(removed)}")
    if not_found:
        report.append(f"⚠️ Not found (ids: {', '.join(str(i) for i in not_found)})")
    if stale:
        # Internal signal: the model is expected to call show_list and retry.
        report.append("Cache stale: " + "; ".join(stale))
    return "\n".join(report) or "Nothing to remove."


@tool
def show_list(scope: _SCOPE_ARG = "personal") -> str:
    """Show all items currently on a shopping list."""
    items = storage.get_items(_owner_for_scope(scope))
    user_id = _current_user_id.get()
    if user_id is not None:
        _cache_view(user_id, scope, items)
    return _render_list(_LIST_TITLES[scope], items)


@tool
def clear_list(scope: _SCOPE_ARG = "personal") -> str:
    """Clear all items from a shopping list."""
    count = storage.clear_list(_owner_for_scope(scope))
    _refresh_user_cache()
    target = _LIST_TARGETS[scope]
    if count == 0:
        return f"Nothing to clear — {target} was already empty."
    plural = "" if count == 1 else "s"
    return f"🗑️ Cleared {count} item{plural} from {target}."


class AgentError(Exception):
    def __init__(self, user_message: str, admin_detail: str):
        super().__init__(admin_detail)
        self.user_message = user_message
        self.admin_detail = admin_detail


_tools = [add_items, remove_items, remove_items_by_id, show_list, clear_list]
_agent = None


def _build_model() -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        google_api_key=os.getenv("GEMINI_API_KEY"),
        temperature=0,
        # Thinking must stay off. With it on, gemini-2.5-flash regularly ends the turn
        # having spent everything on thoughts: 0 output tokens, no tool call, and
        # finish_reason=STOP. Measured on "show my list" phrasings, 6 of 10 failed that
        # way (imperatives like "muestrame mi lista" failed every time); with
        # thinking_budget=0 all 10 succeed and reference-heavy deletes still pass.
        thinking_budget=0,
    )


def _get_agent():
    global _agent
    if _agent is None:
        _agent = create_agent(_build_model(), _tools, system_prompt=SYSTEM_PROMPT)
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


def _message_text(message) -> str:
    content = message.content
    if isinstance(content, list):
        # Gemini mixes thought/tool blocks in with text, and a block may be a plain string.
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return (content or "").strip()


_ID_MARKER = re.compile(r" ?\(id=\d+\)")


def _reply(text: str) -> str:
    """Database ids are internal. The model is told not to echo the [Recently viewed list]
    block, but it occasionally does anyway, so strip them rather than rely on the prompt."""
    return _ID_MARKER.sub("", text).strip()


@traceable(name="extract_response")
def _extract_text(result: dict) -> str:
    """The reply to send. The model sometimes ends a tool-calling turn with no text at all;
    fall back to the last tool's output, which is already written as user-facing copy."""
    messages = result["messages"]
    text = _message_text(messages[-1])
    if text:
        return _reply(text)
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            return _reply(str(message.content))
    return "Done. / Listo."


def format_lists_for(user_id: int) -> str | None:
    """Plain (non-LLM) rendering of a user's personal list + the common list, for the
    scheduled alert. Returns None if both lists are empty (nothing worth pinging about)."""
    personal = storage.get_items(user_id)
    common = storage.get_items(None)
    if not personal and not common:
        return None
    blocks = [
        "🛒 Shopping reminder / Recordatorio de compras",
        _render_list(_LIST_TITLES["personal"], personal),
        _render_list(_LIST_TITLES["common"], common),
    ]
    return "\n\n".join(blocks)


@traceable(name="grocery_bot.run", tags=["telegram"])
async def run(text: str, user: str = "", user_id: int | None = None) -> str:
    message = _build_prompt(text, user, user_id)

    token = _current_user_id.set(user_id)
    try:
        try:
            agent = _get_agent()
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
