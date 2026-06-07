"""
rag/guardrails.py
-----------------
Input sanitisation and jailbreak / prompt-injection detection.

Two concerns are kept separate:
  1. Sanitisation  — clean the input before it touches the LLM
  2. Detection     — flag inputs that attempt to override system behaviour

Both return structured results so the API can log them independently.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Sanitisation
# ------------------------------------------------------------------ #

# Characters / sequences that are meaningful in XML/HTML but not in plain questions
_HTML_ESCAPE = {
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#x27;",
}

# Max question length we'll accept
_MAX_INPUT_LENGTH = 1000


@dataclass
class SanitisedInput:
    original: str
    cleaned: str
    was_truncated: bool
    had_html: bool
    had_null_bytes: bool

    @property
    def was_modified(self) -> bool:
        return self.was_truncated or self.had_html or self.had_null_bytes


def sanitise(text: str) -> SanitisedInput:
    """
    Clean user input:
      - Strip leading/trailing whitespace
      - Remove null bytes
      - Escape HTML special characters
      - Truncate to _MAX_INPUT_LENGTH
    """
    original = text
    had_null_bytes = "\x00" in text
    text = text.replace("\x00", "")

    # Collapse excessive whitespace (but preserve single newlines for multi-line questions)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    # Check for HTML before escaping
    had_html = bool(re.search(r"<[a-zA-Z/!?][^>]*>", text))
    for char, replacement in _HTML_ESCAPE.items():
        text = text.replace(char, replacement)

    was_truncated = len(text) > _MAX_INPUT_LENGTH
    if was_truncated:
        text = text[:_MAX_INPUT_LENGTH].rstrip()
        logger.debug("Input truncated to %d chars", _MAX_INPUT_LENGTH)

    return SanitisedInput(
        original=original,
        cleaned=text,
        was_truncated=was_truncated,
        had_html=had_html,
        had_null_bytes=had_null_bytes,
    )


# ------------------------------------------------------------------ #
# Injection / jailbreak detection
# ------------------------------------------------------------------ #

# Patterns that signal prompt-injection or jailbreak attempts.
# Ordered from most specific to most general.
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    # Classic instruction overrides
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|context)", re.I),
    re.compile(r"disregard\s+(all\s+)?(previous|prior|above)\s+", re.I),
    re.compile(r"forget\s+(everything|all)\s+(you('ve)?\s+been\s+told|above)", re.I),
    # Role / persona hijacking
    re.compile(r"you\s+are\s+now\s+(a|an|the)\s+", re.I),
    re.compile(r"act\s+as\s+(if\s+you\s+are|a|an)\s+", re.I),
    re.compile(r"pretend\s+(you\s+are|to\s+be)\s+", re.I),
    re.compile(r"roleplay\s+as\s+", re.I),
    re.compile(r"your\s+new\s+(role|persona|identity|instructions?)\s+(is|are)", re.I),
    # System prompt exfiltration
    re.compile(r"(show|print|reveal|repeat|output|display)\s+(your\s+)?(system\s+prompt|instructions?|context)", re.I),
    re.compile(r"what\s+(are|were)\s+your\s+(original\s+)?instructions?", re.I),
    # DAN / jailbreak keywords
    re.compile(r"\bDAN\b"),
    re.compile(r"jailbreak", re.I),
    re.compile(r"do\s+anything\s+now", re.I),
    # Delimiter injection
    re.compile(r"<\|?(system|user|assistant|im_start|im_end)\|?>", re.I),
    re.compile(r"\[INST\]|\[/?SYS\]", re.I),
    # Eval / code injection attempts
    re.compile(r"(eval|exec|__import__|subprocess)\s*\(", re.I),
]


@dataclass
class InjectionResult:
    detected: bool
    matched_pattern: str | None   # human-readable description of what fired


def detect_injection(text: str) -> InjectionResult:
    """
    Scan text for prompt-injection / jailbreak patterns.
    Returns InjectionResult(detected=True, matched_pattern=...) if any fire.
    """
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            desc = pattern.pattern[:80]
            logger.warning("Injection pattern matched: %s", desc)
            return InjectionResult(detected=True, matched_pattern=desc)
    return InjectionResult(detected=False, matched_pattern=None)


# ------------------------------------------------------------------ #
# Convenience: run both in one call
# ------------------------------------------------------------------ #

@dataclass
class GuardrailResult:
    sanitised: SanitisedInput
    injection: InjectionResult

    @property
    def safe_text(self) -> str:
        return self.sanitised.cleaned

    @property
    def should_block(self) -> bool:
        return self.injection.detected


def run_guardrails(raw_text: str) -> GuardrailResult:
    """
    Run sanitisation then injection detection on raw user input.
    Always call this before passing input to the RAG pipeline.
    """
    sanitised = sanitise(raw_text)
    # Run injection detection on the *original* text (pre-HTML-escape)
    # so we don't miss patterns that relied on angle brackets
    injection = detect_injection(raw_text)
    if sanitised.was_modified:
        logger.debug(
            "Input modified by guardrails: truncated=%s html=%s null=%s",
            sanitised.was_truncated,
            sanitised.had_html,
            sanitised.had_null_bytes,
        )
    return GuardrailResult(sanitised=sanitised, injection=injection)