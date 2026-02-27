"""Claude client for document classification and obligation verification.

Uses Anthropic's tool_use API for structured output (classification) and
free-form messages for verification tasks.
"""

from __future__ import annotations

import json

import anthropic
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from echelonos.config import settings

log = structlog.get_logger(__name__)


def get_anthropic_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


@retry(
    retry=retry_if_exception_type((anthropic.RateLimitError, anthropic.APIConnectionError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
def extract_with_structured_output(
    client: anthropic.Anthropic,
    system_prompt: str,
    user_prompt: str,
    response_format: type,
):
    """Extract structured data using Claude with tool_use.

    Converts a Pydantic model into a tool schema, forces Claude to call it,
    and parses the result back into the Pydantic model instance.

    Parameters
    ----------
    client:
        An Anthropic client instance.
    system_prompt:
        The system-level instruction.
    user_prompt:
        The user-level content (e.g. document text).
    response_format:
        A Pydantic BaseModel subclass defining the expected output schema.

    Returns
    -------
    An instance of *response_format* populated with Claude's response.
    """
    # Convert Pydantic model to JSON Schema for the tool definition.
    schema = response_format.model_json_schema()

    # Remove unsupported keys that Pydantic may include.
    schema.pop("title", None)

    tool_name = "structured_output"
    tools = [
        {
            "name": tool_name,
            "description": "Return the structured result matching the required schema.",
            "input_schema": schema,
        }
    ]

    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=16384,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
        tools=tools,
        tool_choice={"type": "tool", "name": tool_name},
    )

    # Extract the tool call input from the response.
    for block in response.content:
        if block.type == "tool_use" and block.name == tool_name:
            return response_format.model_validate(block.input)

    # Fallback: should not happen with tool_choice forced, but be safe.
    log.error("claude_no_tool_use_block", response_id=response.id)
    raise ValueError("Claude did not return a tool_use block")


def verify_extraction(
    client: anthropic.Anthropic,
    obligation_text: str,
    source_clause: str,
    raw_text: str,
) -> dict:
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
