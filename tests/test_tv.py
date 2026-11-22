import threading
import time

import tv


class ChattyArt:
    """An art channel that keeps receiving messages but never the reply, until closed."""

    def __init__(self):
        self.closed = threading.Event()

    def get_artmode(self):
        while not self.closed.wait(0.01):
            pass
        raise ConnectionError('socket closed')

    def close(self):
        self.closed.set()


def test_status_gives_up_and_closes_the_connection(monkeypatch):
    art = ChattyArt()
    monkeypatch.setattr(tv, 'connect', lambda ip: art)
    monkeypatch.setattr(tv, 'LIMIT', 0.3)
    monkeypatch.setattr(tv._bounded, '__defaults__', (0.3,))
    started = time.time()
    try:
        tv.status('192.0.2.1')
    except TimeoutError as e:
        assert 'did not finish' in str(e)
    else:
        raise AssertionError('status should time out')
    assert time.time() - started < 2
    assert art.closed.is_set()


def test_bounded_passes_results_and_errors_through():
    assert tv._bounded(lambda opened: 42, [], 1) == 42
    try:
        tv._bounded(lambda opened: 1 / 0, [], 1)
    except ZeroDivisionError:
        pass
    else:
        raise AssertionError('error should reach the caller')
