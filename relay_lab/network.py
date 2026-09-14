"""In-process socket audit for the default Mock policy; records counts, never addresses."""
import ipaddress
import sys
import threading
from contextlib import contextmanager

_state = threading.local()


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
    current = getattr(_state, 'current', None)
    if current is None:
        return
    if event == 'socket.getaddrinfo':
        if not loopback(args[0]):
            current['blocked_external_attempts'] += 1
            raise ValueError('External DNS blocked in Mock mode')
    if event == 'socket.connect':
        address = args[1]
        if isinstance(address, tuple):
            if not loopback(address[0]):
                current['blocked_external_attempts'] += 1
                raise ValueError('External socket blocked in Mock mode')
            current['loopback_connections'] += 1


sys.addaudithook(audit)


@contextmanager
def mock_network_guard(evidence):
    previous = getattr(_state, 'current', None)
    _state.current = evidence
    try:
        yield
    finally:
        if previous is None:
            del _state.current
        else:
            _state.current = previous
