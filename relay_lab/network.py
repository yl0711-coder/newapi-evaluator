"""In-process socket audit for the default Mock policy; records counts, never addresses."""
import ipaddress
import sys
from contextlib import contextmanager

_current = None


def loopback(host):
    if isinstance(host, bytes):
        host = host.decode('ascii')
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def audit(event, args):
    if _current is None:
        return
    if event == 'socket.getaddrinfo':
        if not loopback(args[0]):
            _current['blocked_external_attempts'] += 1
            raise ValueError('External DNS blocked in Mock mode')
    if event == 'socket.connect':
        address = args[1]
        if isinstance(address, tuple):
            if not loopback(address[0]):
                _current['blocked_external_attempts'] += 1
                raise ValueError('External socket blocked in Mock mode')
            _current['loopback_connections'] += 1


sys.addaudithook(audit)


@contextmanager
def mock_network_guard(evidence):
    global _current
    previous = _current
    _current = evidence
    try:
        yield
    finally:
        _current = previous
