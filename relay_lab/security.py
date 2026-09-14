import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit, urlunsplit


def target_url(value, confirm_live=False):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError('Invalid target URL') from None
    if parsed.scheme not in ('http', 'https') or not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Target URL must not contain credentials, query or fragment')
    if not confirm_live:
        if host == 'localhost':
            host = '127.0.0.1'
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback or parsed.scheme != 'http':
            raise ValueError('Live target requires --confirm-live')
    authority = '[' + host + ']' if ':' in host else host
    if port:
        authority += ':' + str(port)
    return urlunsplit((parsed.scheme, authority, parsed.path.rstrip('/'), '', ''))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def redact(value):
    """Defense in depth. Reports still use allowlisted schemas, not arbitrary payloads."""
    if isinstance(value, dict):
        return {str(k): ('[REDACTED]' if re.search(r'key|token|secret|authorization|cookie|prompt|content|body|url', str(k), re.I)
                         else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'https?://\S+', '[URL]', value)
        value = re.sub(r'(?i)(\bbearer\s+|(?<![A-Za-z0-9_-])sk-)[A-Za-z0-9._-]+', '[REDACTED]', value)
        value = re.sub(r'(?i)(api[_-]?key|token|secret|cookie|password)\s*[:=]\s*[^\s,;]+', r'\1=[REDACTED]', value)
    return value
