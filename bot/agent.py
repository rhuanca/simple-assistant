import contextvars
import os
import re
from typing import Annotated, Literal

from cachetools import TTLCache
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langsmith import traceable
from pydantic import Field

from bot import localtime, storage

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

To remove items, pick the tool that matches how the user referred to them, and call it
straight away — never answer with a question you could have answered with a tool call:
  - By number, position or ordinal ("borra el 2", "drop #7", "the third one", "delete 1")
    → remove_items_by_number with those numbers. Do NOT call show_list first: the tool
    resolves the numbers itself against the current list.
  - By name ("quita el jamón", "remove the cheese") → remove_items with the item names.

A [Recently viewed list] block may appear before the user's message, showing the rows the
user was last shown and which list they came from. It is internal scratch data: never quote
it, and never treat it as the answer to a request to see a list — to show a list, always
call show_list and reproduce its output. It is only a hint about what the user is looking
at; the two tools above are still the way to delete, and the block is often absent or out
of date, so never wait for it.

The bot handles two separate things: shopping lists, and the sender's appointments.
Appointments are always personal — there is no shared appointment schedule.

Every message starts with a [Now: ...] line giving the current local date, time and
timezone. Resolve every relative date against it ("next Sunday", "el próximo domingo",
"mañana", "in two weeks") and pass the result to add_appointment as YYYY-MM-DDTHH:MM.
  - If the user names a day but no time, ASK what time it is and do not save anything yet.
    Only call add_appointment once you know the time.
  - Always state the date you worked out back to the user, so a mistake is obvious.
  - To cancel, pass words from the title to cancel_appointment. If the user points at one
    by position ("the second one", "la segunda"), call list_appointments first and take the
    title from that row.

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

# Short-term conversation memory, so a follow-up like "a las 3" still makes sense after the
# bot asked what time an appointment is. Deliberately small and short-lived: enough to
# finish an exchange, not enough for stale context to start steering later answers.
_HISTORY_EXCHANGES = 3
_history: TTLCache = TTLCache(maxsize=1000, ttl=10 * 60)
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
            {"number": i, "text": item["item_text"]}
            for i, item in enumerate(raw_items, 1)
        ],
    }


def _get_cached_view(user_id: int) -> dict | None:
    return _view_cache.get(user_id)


def clear_view_cache() -> None:
    """Drop every cached list view and conversation. Needed when the database is rebuilt
    underneath us, so no one can reference ids that belonged to the old data."""
    _view_cache.clear()
    _history.clear()


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
    lines = [f"  {it['number']}. {it['text']}" for it in view["items"]]
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
    items: Annotated[list[str], Field(description="Item names to remove, e.g. ['leche']. Use this whenever the user names what to remove rather than pointing at a row number.")],
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
def remove_items_by_number(
    numbers: Annotated[
        list[int], Field(description="Row numbers exactly as shown to the user, starting at 1.")
    ],
    scope: _SCOPE_ARG = "personal",
) -> str:
    """Remove items by their position in the list — "delete 1", "borra el 2", "the third one".

    Resolves the numbers against the list as it stands right now, so it needs no ids, never
    goes stale, and works in a single call with nothing looked up beforehand.
    """
    owner = _owner_for_scope(scope)
    items = storage.get_items(owner)
    acting_user_id = _current_user_id.get()

    targets, missing = [], []
    # Resolve every number against one snapshot before deleting anything, so the positions
    # cannot shift underneath a multi-item request.
    for number in dict.fromkeys(numbers):
        if 1 <= number <= len(items):
            targets.append(items[number - 1])
        else:
            missing.append(number)

    removed = [
        item["item_text"]
        for item in targets
        if storage.remove_item_by_id(item["id"], acting_user_id)
    ]
    _refresh_user_cache()

    report = []
    if removed:
        report.append(f"✅ Removed from {_LIST_TARGETS[scope]}: {', '.join(removed)}")
    if missing:
        plural = "" if len(items) == 1 else "s"
        report.append(
            f"⚠️ There is no item {', '.join(str(n) for n in missing)} — "
            f"{_LIST_TARGETS[scope]} has {len(items)} item{plural}."
        )
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


# --- Appointments -----------------------------------------------------------

_NO_USER = "I could not tell who you are, so I cannot manage your appointments."


def _render_appointments(title: str, appointments: list[dict]) -> str:
    lines = [title]
    for i, appointment in enumerate(appointments, 1):
        when = localtime.format_local(appointment["starts_at"])
        lines.append(f"{i}. {when} — {appointment['title']}")
    return "\n".join(lines)


@tool
def add_appointment(
    title: Annotated[
        str, Field(description="What the appointment is for, e.g. 'doctor de gastroenterología'.")
    ],
    starts_at: Annotated[
        str,
        Field(
            description="When it starts, in local time, as 'YYYY-MM-DDTHH:MM'. Work out "
            "relative dates like 'next Sunday' from the [Now] line."
        ),
    ],
) -> str:
    """Save an appointment for the sender. Call this only once you know both what the
    appointment is for and what time it starts — if the user gave a day but no time, ask
    them for the time instead of guessing."""
    user_id = _current_user_id.get()
    if user_id is None:
        return _NO_USER
    try:
        moment = localtime.parse_local(starts_at)
    except ValueError:
        return f"I could not read '{starts_at}' as a date and time. Use YYYY-MM-DDTHH:MM."
    storage.add_appointment(title, localtime.to_storage(moment), owner_user_id=user_id)
    return f"📅 Saved: {localtime.format_local(moment)} — {title}"


@tool
def list_appointments() -> str:
    """Show the sender's upcoming appointments, soonest first."""
    user_id = _current_user_id.get()
    if user_id is None:
        return _NO_USER
    now = localtime.to_storage(localtime.now_local())
    appointments = storage.get_upcoming_appointments(user_id, now)
    if not appointments:
        return "📅 You have no upcoming appointments."
    return _render_appointments(f"📅 Upcoming appointments — {len(appointments)}", appointments)


@tool
def cancel_appointment(
    title: Annotated[
        str, Field(description="Words from the appointment's title, e.g. 'doctor'.")
    ],
) -> str:
    """Cancel one upcoming appointment, found by words from its title. If the user refers to
    one by position ("the second one", "la segunda"), call list_appointments first and pass
    the title from that row."""
    user_id = _current_user_id.get()
    if user_id is None:
        return _NO_USER
    now = localtime.to_storage(localtime.now_local())
    matches = storage.find_upcoming_appointments(title, user_id, now)
    if not matches:
        return f"⚠️ No upcoming appointment matches '{title}'."
    if len(matches) > 1:
        return _render_appointments(
            f"Several appointments match '{title}' — which one do you mean?", matches
        )
    appointment = matches[0]
    storage.cancel_appointment(appointment["id"], user_id)
    when = localtime.format_local(appointment["starts_at"])
    return f"🗑️ Cancelled: {when} — {appointment['title']}"


class AgentError(Exception):
    def __init__(self, user_message: str, admin_detail: str):
        super().__init__(admin_detail)
        self.user_message = user_message
        self.admin_detail = admin_detail


_tools = [
    add_items,
    remove_items,
    remove_items_by_number,
    show_list,
    clear_list,
    add_appointment,
    list_appointments,
    cancel_appointment,
]
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
    # Without this the model has no idea what day it is and cannot resolve "next Sunday".
    # It belongs in the per-turn message, not the cached system prompt.
    now = localtime.now_local()
    parts = [f"[Now: {localtime.format_local(now)} ({storage.get_setting('timezone')})]"]
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
    history = _history.get(user_id, []) if user_id is not None else []
    # Only the raw exchanges are replayed — re-sending old [Now]/[Recently viewed] blocks
    # would feed the model stale state.
    messages = []
    for past_text, past_reply in history:
        messages.append(HumanMessage(content=past_text))
        messages.append(AIMessage(content=past_reply))
    messages.append(HumanMessage(content=message))

    token = _current_user_id.set(user_id)
    try:
        try:
            agent = _get_agent()
            result = await agent.ainvoke({"messages": messages})
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

    reply = _extract_text(result)
    if user_id is not None:
        _history[user_id] = (history + [(text, reply)])[-_HISTORY_EXCHANGES:]
    return reply
