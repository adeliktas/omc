"""Public numeric task allowances are metadata, never credential exemptions."""
from app.core.evidence_redaction import redact_evidence


def test_budget_counts_preserve_evidence_without_exempting_credentials():
    for name in ("budget_tokens", "max_tokens_budget"):
        for text in (f"{name}=100", f"{{'{name}': 100}}", f'{{"{name}": 100}}'):
            assert redact_evidence(text) == text
        for value in ('"100"', '"private-value"', '100 + "private-value"', '100\n+ "private-value"'):
            text = f"{name}={value}"
            assert redact_evidence(text) != text
            assert "private-value" not in redact_evidence(text)
    for text in ('auth_token=100', 'api_key=100', 'password=100', 'budget_tokens="sk-123456789012345678901234"'):
        assert redact_evidence(text) != text
