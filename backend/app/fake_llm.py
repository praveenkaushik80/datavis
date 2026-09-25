"""Deterministic offline chat model (LLM_PROVIDER=fake).

Lets the whole platform run without an OpenRouter key: the research agent gets heuristic descriptions and
the question-answering agent follows a fixed plan (list tables -> query the best matching table -> answer).
Never use in production.
"""
import json
import os
import re
import time
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from .jsonutil import content_text, extract_json


def _humanize(name: str) -> str:
    return re.sub(r"[_\s]+", " ", name.split(".")[-1]).strip()


class OfflineChatModel(BaseChatModel):
    purpose: str = "agent"
    tool_names: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "datafusion-offline"

    def bind_tools(self, tools: list, **kwargs: Any):
        names = [getattr(t, "name", None) or t.get("name") for t in tools]
        return self.model_copy(update={"tool_names": names})

    # ---- research: heuristic metadata --------------------------------------------------------
    def _research(self, prompt: str) -> str:
        m = re.search(r"TABLES_JSON:\s*(\[.*?\])\s*END_TABLES", prompt, re.S)
        if "KNOWLEDGE_REQUEST" in prompt:
            tables = json.loads(m.group(1)) if m else []
            glossary = [{"term": _humanize(t["name"]).title(),
                         "definition": f"Business records held in {t['name']}.", "related": [t["name"]]}
                        for t in tables]
            rules = [{"rule": f"{t['name']}.{c['name']} is required.", "tables": [t["name"]], "source": "schema"}
                     for t in tables for c in t["columns"] if not c.get("nullable", True) and not c.get("pk")][:20]
            return json.dumps({"business_rules": rules, "glossary": glossary, "notes": "offline heuristics"})
        tables = json.loads(m.group(1)) if m else []
        out = {}
        for t in tables:
            cols = {c["name"]: {"description": c.get("comment") or f"The {_humanize(c['name'])} of the "
                                f"{_humanize(t['name'])} record.", "business_meaning": ""} for c in t["columns"]}
            out[t["name"]] = {"description": t.get("description") or f"One row per {_humanize(t['name'])}.",
                              "business_meaning": "", "columns": cols}
        return json.dumps({"tables": out})

    # ---- agent: fixed plan -------------------------------------------------------------------
    def _agent(self, messages: list[BaseMessage]) -> AIMessage:
        question = next((m.content for m in messages if m.type == "human"), "")
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        find = lambda suffix: next((n for n in self.tool_names if n.endswith(suffix)), None)  # noqa: E731
        if not tool_msgs and find("list_tables"):
            return AIMessage(content="", tool_calls=[{"name": find("list_tables"), "args": {}, "id": "call_1"}])
        if len(tool_msgs) == 1 and find("run_readonly_query"):
            listing = extract_json(content_text(tool_msgs[0].content)) or {}
            tables = [t["table"] for t in listing.get("tables", [])]
            words = set(re.findall(r"[a-z]+", str(question).lower()))
            best = max(tables, key=lambda t: len(words & set(re.findall(r"[a-z]+", t.lower()))), default=None)
            if best:
                return AIMessage(content="", tool_calls=[{"name": find("run_readonly_query"),
                                                          "args": {"sql": f"SELECT * FROM {best}", "limit": 5},
                                                          "id": "call_2"}])
        last = extract_json(content_text(tool_msgs[-1].content)) if tool_msgs else {}
        rows = last.get("rows", [])
        answer = {"answer": f"Offline mode: returned {len(rows)} row(s) from {last.get('source', 'the database')}.",
                  "reasoning": "Listed the permitted tables, picked the table whose name best matches the question, "
                               "and ran a read-only sample query.",
                  "confidence": "low", "limitations": "Offline demo model; answers are not analytical."}
        return AIMessage(content=json.dumps(answer))

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        delay = float(os.environ.get("DATAFUSION_FAKE_LLM_DELAY", "0") or 0)   # simulate slow models in tests
        if delay:
            time.sleep(delay)
        if self.purpose == "research":
            msg = AIMessage(content=self._research("\n".join(str(m.content) for m in messages)))
        else:
            msg = self._agent(messages)
        msg.usage_metadata = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        return ChatResult(generations=[ChatGeneration(message=msg)])
