"""Step-level checks shared by the SDK's expectations and the server's evaluation.

  arg_problems      a tool call's arguments against the tool's own input schema (the definition the
                    model was given): required fields missing, wrong types, values outside an enum,
                    fields the schema doesn't have
  claims_success    whether an answer says an action was done ("Your order has been cancelled")
  says_it_failed    whether an answer tells the user something didn't work
  abstains          whether an answer says its sources don't answer the question ("the documents
                    don't say"), instead of giving an answer
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_TYPES = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,), "array": (list, tuple),
          "object": (dict,), "null": (type(None),)}


# The tool's description, kept in its schema under a keyword JSON Schema ignores: the model reads it
# to choose a tool, so a reworded description is a change to the tool. Servers that predate it store
# it like any other keyword.
DESCRIPTION = "x-assay-description"


def tool_schemas(tools: Optional[List[Any]]) -> Dict[str, dict]:
    """{name: input schema} from tool definitions as providers take them: Anthropic's input_schema,
    OpenAI's function parameters (or a flat {"name", "parameters"}), MCP's inputSchema. The tool's
    description, when it has one, goes in as DESCRIPTION."""
    out = {}
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name")
        schema = fn.get("input_schema") or fn.get("parameters") or fn.get("inputSchema")
        text = fn.get("description")
        if name and isinstance(schema, dict):
            if isinstance(text, str) and text:
                schema = {**schema, DESCRIPTION: text[:4096]}
            out[str(name)[:128]] = schema
    return out


def tool_collisions(tools: Optional[List[Any]]) -> Dict[str, List[str]]:
    """Detect tool names offered by more than one MCP server in the same model call:
    returns {tool_name: [server1, server2, ...]} for each colliding tool."""
    if not tools:
        return {}
    by_tool: Dict[str, List[str]] = {}
    for t in tools:
        name, server = None, None
        if isinstance(t, str):
            if "." in t:
                server, name = t.split(".", 1)
            else:
                name = t
        elif isinstance(t, dict):
            fn = t.get("function") if isinstance(t.get("function"), dict) else t
            name = fn.get("name")
            server = t.get("server") or t.get("mcp_server") or fn.get("server") or fn.get("mcp_server")
            if name and "." in str(name) and not server:
                server, name = str(name).split(".", 1)
        if name and server:
            servers = by_tool.setdefault(str(name), [])
            if str(server) not in servers:
                servers.append(str(server))
    return {name: sorted(servers) for name, servers in by_tool.items() if len(servers) > 1}


def _type_ok(value: Any, kind: Any) -> bool:
    kinds = kind if isinstance(kind, list) else [kind]
    for k in kinds:
        py = _TYPES.get(k)
        if py is None:
            return True  # a type this checker doesn't know: not a problem it can see
        if isinstance(value, bool) and k in ("integer", "number"):
            continue
        if isinstance(value, py):
            return True
    return False


def arg_problems(schema: Optional[dict], args: Any, path: str = "") -> List[str]:
    """What's wrong with the arguments, as the schema says: ["order_id is missing", ...]. [] when fine."""
    if not isinstance(schema, dict):
        return []
    out = []
    if schema.get("type") and not _type_ok(args, schema["type"]):
        return [f"{path or 'the arguments'} should be {schema['type']}, got {type(args).__name__}"]
    if "enum" in schema and args not in schema["enum"]:
        return [f"{path or 'the value'} is {args!r}, not one of {', '.join(map(repr, schema['enum'][:8]))}"]
    if isinstance(args, dict):
        props = schema.get("properties") or {}
        for k in schema.get("required") or []:
            if k not in args or args[k] is None:
                out.append(f"{path + k} is missing")
        if schema.get("additionalProperties") is False:
            out += [f"{path + k} isn't a field the tool takes" for k in args if k not in props]
        for k, v in args.items():
            if k in props and v is not None:
                out += arg_problems(props[k], v, f"{path + k}.")
    elif isinstance(args, (list, tuple)) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(args[:50]):
            out += arg_problems(schema["items"], v, f"{path}{i}.")
    return [p.replace(". should", " should").replace(". is ", " is ").rstrip(".") for p in out]


_DONE = re.compile(r"\b(?:successfully|has been|have been|is now|was|were|I've|I have|we've)\s+(?:\w+\s+){0,2}?"
                   r"(?:cancel+ed|refunded|booked|scheduled|sent|deleted|removed|updated|changed|confirmed|processed|"
                   r"completed|created|submitted|placed|paid|issued|reset|added|saved|approved|transferred)\b|"
                   r"\b(?:cancel+ed|refunded|booked|scheduled|sent|deleted|processed|issued|submitted) (?:it|your|the)\b|"
                   r"\b(?:done|all set)\b[.!]", re.I)
_NOT = re.compile(r"\b(?:not|n't|couldn't|could not|unable|failed|fail|won't|cannot|can't|wasn't|haven't|hasn't|"
                  r"didn't|error|problem|unfortunately|sorry)\b", re.I)
_FAILED = re.compile(r"\b(?:couldn't|could not|unable to|wasn't able|weren't able|failed|isn't available|"
                     r"not available|unavailable|no results|didn't find|did not find|couldn't find|can't|cannot|"
                     r"something went wrong|try again|error|sorry|unfortunately|having trouble|no longer)\b", re.I)


def _sentences(text: str) -> List[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def claims_success(answer: Optional[str]) -> Optional[str]:
    """The sentence where the answer says an action was done, or None. A sentence that says it wasn't
    ("couldn't be cancelled") isn't a claim."""
    for s in _sentences(answer or ""):
        if _DONE.search(s) and not _NOT.search(s):
            return s.strip()[:200]
    return None


def says_it_failed(answer: Optional[str]) -> bool:
    return bool(_FAILED.search(answer or ""))


_SOURCES = (r"(?:documents?|sources?|context|passages?|materials?|records?|knowledge base|excerpts?|"
            r"search results|information (?:provided|available|given|I have|I was given))")
_ABSTAIN = re.compile(
    r"\b" + _SOURCES + r"\b[^.!?]{0,40}?\b(?:do(?:es)?n't|do(?:es)? not|did(?:n't| not))\s+(?:\w+\s+)?"
    r"(?:say|mention|cover|contain|include|specify|state|address|provide|discuss|answer|give|list)\b|"
    r"\b(?:isn't|is not|aren't|are not|wasn't|was not|not)\s+(?:\w+\s+)?(?:mentioned|covered|specified|stated|"
    r"addressed|included|found|given|listed)\s+in\s+(?:the|these|your|any|my)\b|"
    r"\bI(?:'m| am)?\s*(?:don't|do not|couldn't|could not|can't|cannot|wasn't able to|was unable to|am unable to|"
    r"'m unable to|not able to)\s+(?:find|see|locate|determine|answer|tell|confirm)\b|"
    r"\bI (?:don't|do not) (?:have|know)\b(?! (?:any )?(?:access|permission))|"
    r"\b(?:no|not enough|insufficient) (?:information|mention|details?|data|record|evidence)\b[^.!?]{0,40}?"
    r"\b(?:about|on|regarding|of|for|to answer|in)\b|"
    r"\b(?:unable|not able) to (?:answer|find|determine|confirm)\b|\b(?:can't|cannot) answer\b", re.I)


def abstains(answer: Optional[str]) -> Optional[str]:
    """The sentence where the answer says its sources don't answer the question ("The documents
    don't say", "I couldn't find anything about"), or None when it answers."""
    for s in _sentences((answer or "").replace("\u2019", "'")):
        if _ABSTAIN.search(s):
            return s.strip()[:200]
    return None
