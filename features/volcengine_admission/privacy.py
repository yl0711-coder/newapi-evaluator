"""Reject caller text echoed into upstream identifiers, URLs or media metadata."""
import codecs
from dataclasses import dataclass
import html
import json
import re
from urllib.parse import unquote_plus

from .catalog import fingerprint

MEDIA_TEXT_CHUNK = 64 * 1024
MAX_MEDIA_TEXT_RUN = 256 * 1024
MAX_MEDIA_HASH_WORK = 16 * 1024 * 1024
MEDIA_TEXT = re.compile(r"[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]+")
UNICODE_ESCAPE = re.compile(r"\\u[dD][89aAbB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}|\\u[0-9a-fA-F]{4}")


class PrivacyScanLimit(ValueError):
    pass


@dataclass
class ScanBudget:
    remaining: int = MAX_MEDIA_HASH_WORK

    def consume(self, amount):
        self.remaining -= amount
        if self.remaining < 0:
            raise PrivacyScanLimit("media_privacy_check_inconclusive")


def decode_unicode_escape(match):
    value = json.loads('"' + match[0] + '"')
    return match[0] if any(0xD800 <= ord(c) <= 0xDFFF for c in value) else value


def text_forms(value):
    forms, current = {value}, value
    for _ in range(4):
        decoded = html.unescape(unquote_plus(current))
        decoded = UNICODE_ESCAPE.sub(decode_unicode_escape, decoded)
        decoded = decoded.replace("\\/", "/")
        forms.add(decoded)
        if decoded == current:
            break
        current = decoded
    return forms


def contains_caller_text(value, *, secrets=(), prompt_digest=None, prompt_length=None, budget=None):
    """The digest/length path works after restart without retaining the original prompt."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return False
    for form in text_forms(value):
        if any(secret and secret in form for secret in secrets):
            return True
        if prompt_digest and type(prompt_length) is int and 0 < prompt_length <= len(form):
            for offset in range(len(form) - prompt_length + 1):
                if budget is not None:
                    budget.consume(prompt_length)
                if fingerprint(form[offset:offset + prompt_length]) == prompt_digest:
                    return True
    return False


def media_contains_caller_text(value, *, secrets=(), prompt_digest=None, prompt_length=None):
    """Scan the complete cleaned media, including text crossing chunk boundaries.

    Invalid UTF-8 bytes and control bytes separate text runs. Inputs exclude those
    characters, so they cannot split a caller string. Large text runs or excessive
    digest work fail closed rather than allocating or hashing unbounded metadata.
    """
    lengths = [len(secret) for secret in secrets if secret]
    if prompt_digest and type(prompt_length) is int and prompt_length > 0:
        lengths.append(prompt_length)
    if not lengths:
        return False
    minimum, pending, budget = min(lengths), "", ScanBudget()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="surrogateescape")
    for offset in range(0, len(value) + MEDIA_TEXT_CHUNK, MEDIA_TEXT_CHUNK):
        final = offset >= len(value)
        text = pending + decoder.decode(value[offset:offset + MEDIA_TEXT_CHUNK], final=final)
        pending = ""
        for match in MEDIA_TEXT.finditer(text):
            candidate = match[0]
            if len(candidate) > MAX_MEDIA_TEXT_RUN:
                raise PrivacyScanLimit("media_privacy_check_inconclusive")
            if match.end() == len(text) and not final:
                pending = candidate
            elif len(candidate) >= minimum and contains_caller_text(candidate, secrets=secrets,
                    prompt_digest=prompt_digest, prompt_length=prompt_length, budget=budget):
                return True
        if final:
            break
    return False
