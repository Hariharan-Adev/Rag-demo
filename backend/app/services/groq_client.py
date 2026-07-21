"""Groq chat-completion client."""

from groq import Groq

from app.config import settings
from app.prompts.rag_prompt import RAG_SYSTEM_PROMPT
from app.utils.security import guard_model_output


def generate_answer(prompt: str) -> dict[str, int | str]:
    """Generate an answer using the configured Groq model."""
    placeholder_values = {
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
