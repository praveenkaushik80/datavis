"""LLM access. All models are served through OpenRouter (via the LiteLLM gateway by default).

`llm_provider=fake` returns a deterministic offline model so the full flow can be demoed and tested
without an API key.
"""
from langchain_core.language_models import BaseChatModel

from .config import get_settings


def chat_model(purpose: str = "agent") -> BaseChatModel:
    s = get_settings()
    if s.llm_provider == "fake":
        from .fake_llm import OfflineChatModel
        return OfflineChatModel(purpose=purpose)

    from langchain_openai import ChatOpenAI

    headers, extra = {}, {}
    if "openrouter.ai" in s.llm_base_url:
        # Direct OpenRouter (e.g. on Vercel, without the LiteLLM gateway): attribution headers, and
        # OpenRouter's own fallback routing. Budgets: set a credit limit on the OpenRouter API key.
        headers = {"HTTP-Referer": s.openrouter_app_url, "X-Title": s.openrouter_app_name}
        fallbacks = [m.strip() for m in s.llm_fallback_models.split(",") if m.strip()]
        if fallbacks:
            extra["models"] = fallbacks
    model = s.llm_model_agent if purpose == "agent" else s.llm_model_research
    return ChatOpenAI(model=model, base_url=s.llm_base_url, api_key=s.llm_api_key, temperature=s.llm_temperature,
                      timeout=s.llm_timeout_seconds, max_retries=2, default_headers=headers or None,
                      extra_body=extra or None)
