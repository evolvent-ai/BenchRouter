"""BenchRouter SDK — optional helpers for benchmark authors."""

import os
from typing import Optional


def create_model_client(base_url: Optional[str] = None, api_key: Optional[str] = None):
    """Create an OpenAI-compatible client from environment variables.

    This is a convenience helper. Benchmark authors can also use the OpenAI SDK
    directly — just read BENCHROUTER_MODEL_ENDPOINT from the environment.
    """
    from openai import OpenAI

    return OpenAI(
        base_url=base_url or os.environ.get("BENCHROUTER_MODEL_ENDPOINT", "http://localhost:8000/v1"),
        api_key=api_key or os.environ.get("OPENAI_API_KEY", "EMPTY"),
    )
