"""Bundled general-purpose MCP tool server (stdio transport).

The default local MCP server for Enterprise Copilot's agent mode (see
app/mcp_tools.py, MCP_STDIO_COMMAND/MCP_STDIO_ARGS in app/config.py) — a
small set of safe, self-contained utility tools with no filesystem,
network, or subprocess access of their own, so it's safe to enable by
default (dev-only, per docs/security.md, same posture as every other local
tool in this app).

Run standalone for a quick manual check: `python mcp_servers/general_tools_server.py`
(reads/writes MCP JSON-RPC frames over stdio — not meant to be run
interactively, but it will sit and wait for a client instead of erroring).
"""
from __future__ import annotations

import ast
import operator
import uuid
from datetime import datetime, timezone as _timezone

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "enterprise-copilot-general-tools",
    instructions="General-purpose utility tools: current date/time, arithmetic, text stats, UUIDs.",
)


@mcp.tool()
def current_datetime() -> str:
    """Current date and time in UTC, ISO 8601. UTC-only (no IANA timezone
    database bundled — see zoneinfo's platform note) so this works
    identically everywhere this app runs, including Windows."""
    return datetime.now(_timezone.utc).isoformat()


# Safe arithmetic evaluator: a fixed, explicit whitelist of AST node types
# and operators — never Python's own eval()/exec(), which would let a
# calculator expression run arbitrary code. Deliberately numeric-only (no
# names, attributes, calls, subscripts, comprehensions).
_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError(f"Unsupported expression: {ast.dump(node)}")


@mcp.tool()
def calculator(expression: str) -> str:
    """Evaluates a basic arithmetic expression (+ - * / // % ** and
    parentheses only — no variables, function calls, or other Python
    syntax). Returns the result as a string, or an error message if the
    expression can't be parsed/evaluated safely."""
    try:
        tree = ast.parse(expression, mode="eval")
        result = _safe_eval(tree)
    except (SyntaxError, ValueError, ZeroDivisionError, TypeError) as exc:
        return f"Error: {exc}"
    return str(result)


@mcp.tool()
def word_count(text: str) -> dict:
    """Basic text statistics: word/character/line counts."""
    return {
        "words": len(text.split()),
        "characters": len(text),
        "characters_no_spaces": len(text.replace(" ", "")),
        "lines": len(text.splitlines()) or (1 if text else 0),
    }


@mcp.tool()
def generate_uuid() -> str:
    """A fresh random UUID4 string."""
    return str(uuid.uuid4())


if __name__ == "__main__":
    mcp.run(transport="stdio")
