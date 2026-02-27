"""Claude client for obligation verification."""

import anthropic

from echelonos.config import settings


def get_anthropic_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def verify_extraction(client: anthropic.Anthropic, obligation_text: str, source_clause: str, raw_text: str) -> dict:
    """Verify an extracted obligation using Claude as independent verifier."""
    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": (
                    "You are verifying an obligation extracted from a contract by another AI.\n\n"
                    f"Extracted obligation: {obligation_text}\n"
                    f"Cited source clause: {source_clause}\n\n"
                    f"Original document text:\n{raw_text}\n\n"
                    "Verify:\n"
                    "1. Does the source clause exist verbatim in the document?\n"
                    "2. Does the obligation accurately reflect the clause?\n"
                    "3. Is the obligation type correct?\n\n"
                    'Respond with JSON: {"verified": bool, "confidence": float, "reason": str}'
                ),
            }
        ],
    )
    return response
