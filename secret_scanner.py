"""
secret_scanner.py — gitleaks-subset scanner for hermem ground-truth payloads.

Design rules (ported from Claude Code's teamMemorySync/secretScanner.ts):
  - High-confidence, prefix-anchored patterns only; generic keyword-context
    rules deliberately omitted (near-zero false positives beats coverage).
  - scan() returns (rule_id, label) pairs only — NEVER the matched text.
  - redact() replaces only the secret with [REDACTED:<rule_id>] so the
    surrounding log/config stays diagnostically useful.
  - DB connection URLs: only the password component is replaced.

Usage:
    from secret_scanner import scan, redact_and_report
    hits = scan(payload)                      # -> [("github-token", "GitHub token"), ...]
    clean, hits = redact_and_report(payload)  # -> redacted payload + rule/label list
"""

import re
from typing import Dict, List, Tuple

# (rule_id, label, compiled regex)
_RULES: List[Tuple[str, str, "re.Pattern[str]"]] = [
    # Cloud
    ("aws-access-key", "AWS access key",
     re.compile(r"\b(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("gcp-api-key", "GCP API key",
     re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("digitalocean-token", "DigitalOcean token",
     re.compile(r"\b(?:dop_v1_|doo_v1_)[a-f0-9]{64}\b")),
    # AI APIs
    ("anthropic-api-key", "Anthropic API key",
     re.compile(r"\bsk-ant-api03-[A-Za-z0-9\-_]{24,}")),
    ("openai-api-key", "OpenAI API key",
     re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}\b")),
    ("huggingface-token", "Hugging Face token",
     re.compile(r"\bhf_[A-Za-z0-9]{34,}\b")),
    # VCS
    ("github-token", "GitHub token",
     re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github-fine-grained-token", "GitHub fine-grained token",
     re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("gitlab-token", "GitLab token",
     re.compile(r"\bglpat-[A-Za-z0-9\-_]{20,}\b")),
    # Comms
    ("slack-token", "Slack token",
     re.compile(r"\bxox[baprsxeap]-[A-Za-z0-9\-]{10,}\b")),
    ("twilio-key", "Twilio API key",
     re.compile(r"\bSK[0-9a-fA-F]{32}\b")),
    ("sendgrid-key", "SendGrid API key",
     re.compile(r"\bSG\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\b")),
    # Dev tooling
    ("npm-token", "npm access token",
     re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("pypi-token", "PyPI upload token",
     re.compile(r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9\-_]{50,}")),
    ("databricks-token", "Databricks token",
     re.compile(r"\bdapi[0-9a-f]{32}\b")),
    ("pulumi-token", "Pulumi access token",
     re.compile(r"\bpul-[A-Za-z0-9]{40,}")),
    ("postman-token", "Postman API key",
     re.compile(r"\bPMAK-[0-9a-f]{24}-[0-9a-f]{32}\b")),
    ("hashicorp-tf-token", "HashiCorp Terraform token",
     re.compile(r"\batlasv1_[A-Za-z0-9]{40,}\b")),
    # Observability
    ("grafana-api-key", "Grafana API key",
     re.compile(r"\bglc_[A-Za-z0-9]{32}\b")),
    ("grafana-sa-token", "Grafana service-account token",
     re.compile(r"\bglsa_[A-Za-z0-9]{32}_[A-Fa-f0-9]{8}\b")),
    ("sentry-user-token", "Sentry user token",
     re.compile(r"\bsntryu_[a-f0-9]{64}\b")),
    ("sentry-ingest-dsn", "Sentry ingest DSN",
     re.compile(r"\bsntrys_[a-f0-9]{64}\b")),
    # Payments / commerce
    ("stripe-key", "Stripe key",
     re.compile(r"\b(?:sk|rk)_(?:test|live|prod)_[A-Za-z0-9]{10,}\b")),
    ("shopify-token", "Shopify token",
     re.compile(r"\b(?:shpat|shpss|shpca|shppa)_[a-fA-F0-9]{32}\b")),
    # Crypto / key material
    ("private-key-block", "PEM private key block",
     re.compile(r"-----BEGIN (?:[A-Z ]*)?PRIVATE KEY-----")),
]

# Connection URLs with embedded password: redact ONLY the password part.
_DB_URL = re.compile(r"(\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|amqps?)://[^:/@\s]+:)([^@\s]{4,})(@)")


def scan(text: str) -> List[Tuple[str, str]]:
    """Return unique (rule_id, label) hits. Never returns matched text."""
    hits: Dict[str, str] = {}
    for rid, label, rx in _RULES:
        if rx.search(text):
            hits[rid] = label
    if _DB_URL.search(text):
        hits["db-connection-password"] = "DB connection password"
    return list(hits.items())


def redact_and_report(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Scan then redact in place. Returns (clean_text, [(rule_id, label), ...]).

    Redaction marker keeps the rule id so future readers know WHAT was here
    without exposing the secret. DB URLs keep scheme/user/host, lose password.
    """
    hits = scan(text)
    clean = text
    for rid, _label, rx in _RULES:
        clean = rx.sub(f"[REDACTED:{rid}]", clean)
    clean = _DB_URL.sub(r"\1[REDACTED:db-connection-password]\3", clean)
    return clean, hits
