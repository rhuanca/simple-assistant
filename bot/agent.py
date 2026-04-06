import os

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI

from bot import storage

SYSTEM_PROMPT = """\
You are a grocery list assistant in a Telegram chat. You help users manage their shopping lists.
The user may write in English or Spanish — always reply in the same language they used.

You have tools to add items, remove items, show a list, and clear a list.
There are two lists: "groceries" (default for food/supermarket items) and "house" \
(for household items — triggered by words like "casa", "hogar", "home", "house").

When the user asks to add or remove items, extract clean item names without articles or filler words.
When you cannot understand the request, ask for clarification in the user's language.
Keep responses short and friendly.
"""


@tool
def add_items(items: list[str], list_name: str = "groceries", added_by: str = "") -> str:
    """Add one or more items to a shopping list.

    Args:
        items: List of item names to add.
        list_name: Which list — "groceries" or "house".
        added_by: Name of the user adding items.
    """
    for item in items:
        storage.add_item(item, list_name=list_name, added_by=added_by)
    return f"Added to {list_name}: {', '.join(items)}"


@tool
def remove_items(items: list[str], list_name: str = "groceries") -> str:
    """Remove one or more items from a shopping list.

    Args:
        items: List of item names to remove.
        list_name: Which list — "groceries" or "house".
    """
    removed = []
    not_found = []
    for item in items:
        if storage.remove_item(item, list_name=list_name):
            removed.append(item)
        else:
            not_found.append(item)
    parts = []
    if removed:
        parts.append(f"Removed: {', '.join(removed)}")
    if not_found:
        parts.append(f"Not found: {', '.join(not_found)}")
    return " | ".join(parts) if parts else "Nothing to remove."


@tool
def show_list(list_name: str = "groceries") -> str:
    """Show all items currently on a shopping list.

    Args:
        list_name: Which list — "groceries" or "house".
    """
    items = storage.get_items(list_name)
    if not items:
        return f"The {list_name} list is empty."
    lines = [f"{list_name} ({len(items)} items):"]
    for i, item in enumerate(items, 1):
        lines.append(f"  {i}. {item['item_text']}")
    return "\n".join(lines)


@tool
def clear_list(list_name: str = "groceries") -> str:
    """Clear all items from a shopping list.

    Args:
        list_name: Which list — "groceries" or "house".
    """
    count = storage.clear_list(list_name)
    return f"Cleared {count} items from {list_name}."


_tools = [add_items, remove_items, show_list, clear_list]
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


async def run(text: str, user: str = "") -> str:
    agent = _get_agent()
    message = text
    if user:
        message = f"[from {user}] {text}"
    result = await agent.ainvoke({"messages": [HumanMessage(content=message)]})
    return result["messages"][-1].content
