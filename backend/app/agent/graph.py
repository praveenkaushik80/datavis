"""Flow B agent (LangGraph): understand -> agent <-> MCP tools -> citation & guardrails -> answer.

Tools come from the auto-generated MCP servers (one per permitted database) plus the Cube MCP server,
loaded through langchain-mcp-adapters. Every answer carries references, sources (executed SQL / Cube
queries), confidence and limitations.
"""
import json
import os
import sys
import time
from contextlib import AsyncExitStack
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp.shared.memory import create_connected_server_and_client_session
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from sqlalchemy import select

from ..config import get_settings
from ..context.service import ContextError, context_payload, get_version
from ..db import Connection, SessionLocal, User
from ..jsonutil import content_text, extract_json
from ..llm import chat_model
from ..observability.telemetry import new_trace_id, record
from ..security.rbac import permitted

SYSTEM = """You are DataFusion's data agent. Answer the user's question from their databases.

How to work:
1. Understand the question and identify the information needed.
2. Use the approved semantic context below and the tools (list_tables, describe_table, search_context,
   query_<table>, run_readonly_query, cube_meta, cube_query) to find it. Prefer cube_query for metrics
   that exist as Cube measures. Queries are read-only.
3. Every number or fact in your answer must come from a tool result. Never invent data.
4. If the data cannot answer the question, say so.

When you are done, reply with JSON only:
{"answer": str, "reasoning": str, "confidence": "high"|"medium"|"low", "limitations": str}

APPROVED SEMANTIC CONTEXT:
"""


class AgentState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    question: str
    user: str
    trace_id: str
    context_refs: list[dict]
    steps: int
    result: dict


def _context_brief(user: User, only: str | None) -> tuple[str, list[dict], list[str]]:
    lines, refs, names = [], [], []
    with SessionLocal() as db:
        for name, access in permitted(db, user).items():
            if only and name != only:
                continue
            conn = db.scalar(select(Connection).where(Connection.name == name))
            try:
                v = get_version(db, conn, "approved")
            except ContextError:
                continue
            allowed = None if access.tables is None else [t for t in v.metadata_layer["tables"] if access.allows_table(t)]
            ctx = context_payload(conn, v, allowed, access.denied_columns)
            names.append(name)
            refs.append({"connection": name, "context_version": v.version, "approved_by": v.decided_by})
            lines.append(f"## database '{name}' ({conn.db_type}), context v{v.version}")
            for tn, t in ctx["metadata_layer"]["tables"].items():
                cols = ", ".join(f"{c}: {cv['description'][:60]}" for c, cv in list(t["columns"].items())[:25])
                lines.append(f"- {tn}: {t['description'][:200]} | {cols}")
            for g in ctx["knowledge_layer"].get("glossary", [])[:30]:
                lines.append(f"  glossary: {g.get('term')} = {g.get('definition', '')[:160]}")
            for r in ctx["knowledge_layer"].get("business_rules", [])[:20]:
                lines.append(f"  rule: {r.get('rule', '')[:200]}")
    return "\n".join(lines), refs, names


def mcp_connections(user_email: str, databases: list[str]) -> dict:
    s = get_settings()
    env = {k: v for k, v in os.environ.items()}
    conns = {f"db_{n}": {"transport": "stdio", "command": sys.executable,
                         "args": ["-m", "app.mcp_servers.db_server", "--connection", n, "--user", user_email],
                         "env": env, "cwd": os.getcwd()} for n in databases}
    if s.cube_api_url:
        conns["cube"] = {"transport": "stdio", "command": sys.executable,
                         "args": ["-m", "app.mcp_servers.cube_server", "--user", user_email],
                         "env": env, "cwd": os.getcwd()}
    return conns


def _guardrails(state: AgentState) -> AgentState:
    """Citation & AI guardrails: attach sources from tool results, downgrade unsupported answers."""
    msgs = state["messages"]
    sources, table, errors = [], None, []
    for m in msgs:
        if not isinstance(m, ToolMessage):
            continue
        body = extract_json(content_text(m.content))
        if body.get("error"):
            errors.append(body["error"])
            continue
        if "rows" in body:
            sources.append({"tool": m.name, "source": body.get("source"), "sql": body.get("sql"),
                            "cube_query": body.get("query"), "row_count": body.get("row_count")})
            rows = body.get("rows") or []
            cols = body.get("columns") or (list(rows[0]) if rows else [])
            table = {"columns": cols, "rows": [[r.get(c) for c in cols] for r in rows[:50]]}
    final = next((m for m in reversed(msgs) if isinstance(m, AIMessage) and not m.tool_calls), None)
    final_text = content_text(final.content) if final else ""
    parsed = extract_json(final_text)
    answer = parsed.get("answer") or final_text
    confidence = parsed.get("confidence", "medium")
    limitations = parsed.get("limitations", "")
    if not sources:
        confidence = "low"
        limitations = (limitations + " " if limitations else "") + \
            "No query results support this answer; treat it as unverified."
    if errors:
        limitations = (limitations + " " if limitations else "") + f"{len(errors)} tool call(s) failed or were blocked."
    if state.get("steps", 0) >= get_settings().agent_max_steps and not parsed:
        answer = answer or "I could not finish within the step limit."
        confidence = "low"
    return {"result": {"answer": answer, "table": table, "reasoning": parsed.get("reasoning", ""),
                       "references": state.get("context_refs", []), "sources": sources,
                       "confidence": confidence, "limitations": limitations.strip(),
                       "blocked_or_failed": errors, "trace_id": state.get("trace_id")}}


def build_agent_graph(tools: list):
    s = get_settings()
    model = chat_model("agent").bind_tools(tools)

    async def agent(state: AgentState) -> AgentState:
        reply = await model.ainvoke(state["messages"])
        record("llm", name="agent", user=state["user"], trace_id=state["trace_id"],
               usage=getattr(reply, "usage_metadata", None))
        return {"messages": [reply], "steps": state.get("steps", 0) + 1}

    def route(state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls and state.get("steps", 0) < s.agent_max_steps:
            return "tools"
        return "guardrails"

    g = StateGraph(AgentState)
    g.add_node("agent", agent)
    g.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    g.add_node("guardrails", _guardrails)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route, {"tools": "tools", "guardrails": "guardrails"})
    g.add_edge("tools", "agent")
    g.add_edge("guardrails", END)
    return g.compile()


async def _inprocess_tools(stack: AsyncExitStack, user_email: str, databases: list[str]) -> list:
    """Same auto-generated MCP servers, connected through in-memory MCP sessions instead of
    subprocesses: the serverless (Vercel) transport. Tool names match the stdio transport."""
    from ..mcp_servers import cube_server, db_server
    servers = {f"db_{n}": db_server.build_server(n, user_email) for n in databases}
    if get_settings().cube_api_url:
        servers["cube"] = cube_server.build_server(user_email)
    tools = []
    for name, server in servers.items():
        session = await stack.enter_async_context(create_connected_server_and_client_session(server))
        tools += await load_mcp_tools(session, server_name=name, tool_name_prefix=True)
    return tools


async def answer_question(question: str, user_email: str, connection: str | None = None) -> dict:
    trace_id, started = new_trace_id(), time.monotonic()
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == user_email))
    if user is None:
        raise PermissionError("Unknown user.")
    brief, refs, databases = _context_brief(user, connection)
    if not databases:
        raise PermissionError("You have no read access to a database with an approved semantic context.")
    status, tools = "ok", []
    stack = AsyncExitStack()
    try:
        if get_settings().effective_mcp_transport == "inprocess":
            tools = await _inprocess_tools(stack, user_email, databases)
        else:
            client = MultiServerMCPClient(mcp_connections(user_email, databases), tool_name_prefix=True)
            tools = await client.get_tools()
        graph = build_agent_graph(tools)
        out = await graph.ainvoke({"messages": [SystemMessage(SYSTEM + brief), HumanMessage(question)],
                                   "question": question, "user": user_email, "trace_id": trace_id,
                                   "context_refs": refs, "steps": 0},
                                  {"recursion_limit": get_settings().agent_max_steps * 2 + 4})
        return out["result"]
    except Exception:
        status = "error"
        raise
    finally:
        await stack.aclose()
        record("agent", name="ask", user=user_email, connection=",".join(databases), status=status,
               trace_id=trace_id, latency_ms=int((time.monotonic() - started) * 1000),
               detail={"question": question[:500], "tools": [t.name for t in tools],
                       "mcp_transport": get_settings().effective_mcp_transport})
