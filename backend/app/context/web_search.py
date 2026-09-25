"""Web search for the research agent. Only runs when switched ON, and only business terms are sent
(table / column names turned into words), never data values or credentials."""
import logging
import re

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)
STOP = {"id", "at", "by", "is", "of", "the", "and", "code", "key", "date", "name", "created", "updated", "type", "no"}


def terms_from_tables(tables: list[dict], limit: int = 8) -> list[str]:
    seen: dict[str, int] = {}
    for t in tables:
        for raw in [t["name"]] + [c["name"] for c in t["columns"]]:
            words = [w for w in re.split(r"[_\W]+", raw.split(".")[-1].lower()) if w and w not in STOP and len(w) > 2]
            if words:
                term = " ".join(words)
                seen[term] = seen.get(term, 0) + 1
    return [t for t, _ in sorted(seen.items(), key=lambda kv: -kv[1])][:limit]


def search_terms(terms: list[str]) -> list[dict]:
    s = get_settings()
    if s.web_search_provider != "tavily" or not s.tavily_api_key:
        return []
    findings = []
    with httpx.Client(timeout=20) as c:
        for term in terms:
            try:
                r = c.post("https://api.tavily.com/search", json={
                    "api_key": s.tavily_api_key, "query": f"business definition of {term}", "max_results": 2})
                r.raise_for_status()
                for item in r.json().get("results", [])[:2]:
                    findings.append({"term": term, "title": item.get("title", ""), "url": item.get("url", ""),
                                     "snippet": (item.get("content") or "")[:400]})
            except httpx.HTTPError as exc:
                log.warning("web search failed for %s: %s", term, exc)
    return findings
