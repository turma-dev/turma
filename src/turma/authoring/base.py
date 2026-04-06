"""Backend interface for provider-specific author generation."""

from __future__ import annotations


class AuthorBackend:
    """Generate planning artifact text from a provider."""

    def generate(self, prompt: str, model: str, timeout: int) -> str:
        """Return the provider's raw text output for the prompt."""
        raise NotImplementedError


def extract_process_error(
    result,
    *,
    provider_name: str,
) -> str:
    """Extract the most useful subprocess failure detail."""
    detail = (
        result.stderr.strip()
        or result.stdout.strip()
        or "unknown error"
    )
    return (
        f"{provider_name} author generation failed: {provider_name} exited with "
        f"{result.returncode}\n{detail}"
    )
