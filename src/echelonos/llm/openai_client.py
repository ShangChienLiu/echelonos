"""GPT-4o client for obligation extraction with structured output."""

from openai import OpenAI

from echelonos.config import settings


def get_openai_client() -> OpenAI:
    return OpenAI(api_key=settings.openai_api_key)


def extract_with_structured_output(client: OpenAI, system_prompt: str, user_prompt: str, response_format: type) -> dict:
    """Extract structured data using GPT-4o with structured output."""
    response = client.beta.chat.completions.parse(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format=response_format,
    )
    return response.choices[0].message.parsed
