"""Backend interface for provider-specific author generation."""

from __future__ import annotations


class AuthorBackend:
    """Generate planning artifact text from a provider."""

    def generate(self, prompt: str, model: str, timeout: int) -> str:
        """Return the provider's raw text output for the prompt."""
        raise NotImplementedError
