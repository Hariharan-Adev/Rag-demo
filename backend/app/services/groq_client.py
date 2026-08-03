"""Chat-completion client for configured LLM providers."""

from groq import Groq
from openai import AzureOpenAI

from app.config import settings
from app.prompts.rag_prompt import RAG_SYSTEM_PROMPT
from app.utils.security import guard_model_output


PLACEHOLDER_VALUES = {
    "",
    "paste_key_1_here",
    "paste_your_groq_api_key_here",
    "paste_a_current_groq_chat_model_id_here",
}


def _configured_provider() -> str:
    """Prefer the explicit provider, otherwise infer from available Azure config."""
    provider = settings.llm_provider.strip().lower()
    if provider:
        return provider
    if settings.azure_openai_endpoint and settings.azure_openai_rag_deployment:
        return "azure_openai"
    return "groq"


def _generate_with_azure(prompt: str) -> dict[str, int | str]:
    """Generate a RAG answer using an Azure OpenAI chat deployment."""
    if (
        settings.azure_openai_endpoint.strip().lower() in PLACEHOLDER_VALUES
        or settings.azure_openai_api_key.strip().lower() in PLACEHOLDER_VALUES
        or settings.azure_openai_rag_deployment.strip().lower() in PLACEHOLDER_VALUES
    ):
        raise ValueError("Azure OpenAI endpoint, API key, or deployment is not configured.")

    client = AzureOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
    )
    response = client.chat.completions.create(
        model=settings.azure_openai_rag_deployment,
        messages=[
            {"role": "system", "content": RAG_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        max_completion_tokens=500,
    )

    answer = response.choices[0].message.content or ""
    usage = response.usage
    return {
        "answer": guard_model_output(answer),
        "prompt_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
        "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
    }


def _generate_with_groq(prompt: str) -> dict[str, int | str]:
    """Generate a RAG answer using the configured Groq model."""
    placeholder_values = PLACEHOLDER_VALUES | {
        "paste_your_groq_api_key_here",
        "paste_a_current_groq_chat_model_id_here",
    }

    if (
        not settings.groq_api_key
        or not settings.groq_model
        or settings.groq_api_key in placeholder_values
        or settings.groq_model in placeholder_values
    ):
        raise ValueError("Groq API key or model is not configured.")

    client = Groq(api_key=settings.groq_api_key)

    response = client.chat.completions.create(
        model=settings.groq_model,
        messages=[
            {
                "role": "system",
                "content": RAG_SYSTEM_PROMPT,
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_completion_tokens=500,
    )

    answer = response.choices[0].message.content or ""
    usage = response.usage

    return {
        "answer": guard_model_output(answer),
        "prompt_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
        "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
    }


def generate_answer(prompt: str) -> dict[str, int | str]:
    """Generate an answer using the configured chat provider."""
    provider = _configured_provider()
    if provider in {"azure", "azure_openai"}:
        return _generate_with_azure(prompt)
    if provider == "groq":
        return _generate_with_groq(prompt)
    raise ValueError("LLM_PROVIDER must be 'azure_openai' or 'groq'.")
