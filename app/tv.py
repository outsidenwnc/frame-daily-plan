"""Talking to the Frame's Art Mode.

Frames differ by model year. The library's standard methods handle newer Frames (2019+,
e.g. a 2022 LS03B) and detect old ones itself. Old Frames (e.g. 2017, Art API 1.07) can
also take one binary WebSocket frame, as the SmartThings app does; that is kept as a
fallback. Each call tries the library first and falls back to the binary way.
"""
import collections
import concurrent.futures
import ipaddress
import json
import os
import socket
import threading
import time
import uuid

import requests
from samsungframetv import SamsungTVWS, exceptions, helper
from samsungframetv import art as _art
from samsungframetv.connection import SamsungTVWSConnection

import config

NAME = 'Frame Daily Plan'

# Right after a new device was allowed, the Frame first announces it on the art channel
# ("ms.channel.clientConnect", carrying the new token) and only then says "ready". The
# library expects "ready" as the very first message and gives up; skip those notices.
_NOTICES = ('ms.channel.clientConnect', 'ms.channel.clientDisconnect')


def _art_open(self):
    if self.connection:
        return self.connection
    SamsungTVWSConnection.open(self)
    for _ in range(10):
        response = helper.process_api_response(self.connection.recv())
        event = response.get('event', '*')
        self._websocket_event(event, response)
        if event == _art.MS_CHANNEL_READY_EVENT:
            return self.connection
        if event not in _NOTICES:
            break
    self.close()
    raise exceptions.ConnectionFailure(response)


_art.SamsungTVArt.open = _art_open

# The last messages the TV sent on the art channel, for the diagnostics. Each recv has a
# timeout, but a TV that keeps sending messages other than the expected reply keeps the
# library waiting forever, so this shows what it was sending.
events = collections.deque(maxlen=20)
_art_event = _art.SamsungTVArt._websocket_event


def _record_event(self, event, response):
    data = response.get('data')
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            pass
    detail = data.get('event') or data.get('request') if isinstance(data, dict) else None
    events.append(f"{time.strftime('%H:%M:%S')} {event}{' / ' + str(detail) if detail else ''}")
    _art_event(self, event, response)


_art.SamsungTVArt._websocket_event = _record_event

# Hard limit for one conversation with the TV. Without it a stuck request keeps one of the
# web server's few threads forever, and after a few the whole web page stops answering.
LIMIT = 60


def _bounded(fn, opened, seconds=LIMIT):
    """Run fn(opened) in a thread; if it takes longer than `seconds`, close every
    connection fn put in `opened` (which ends a blocked recv) and raise TimeoutError."""
    result = {}

    def run():
        try:
            result['value'] = fn(opened)
        except BaseException as e:  # noqa: BLE001  handed to the caller
            result['error'] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        for c in opened:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        raise TimeoutError(f'the TV did not finish answering within {seconds} s')
    if 'error' in result:
        raise result['error']
    return result['value']


def _tv(ip, timeout):
    return SamsungTVWS(host=ip, port=8002, token_file=os.path.join(config.DATA, 'tv-token.txt'),
                       name=NAME, timeout=timeout)


def connect(ip):
    # The TV only asks "allow this device?" on the remote-control channel; an unapproved
    # client on the art channel just gets disconnected. Opening it first triggers the
    # prompt for a new device (and waits long enough for someone to press "Allow").
    auth = _tv(ip, 120)
    auth.open()
    auth.close()
    # art() doesn't inherit the TV's timeout and defaults to 5 s, which is too short for
    # Frames that take a while to answer on the art channel.
    return _tv(ip, 30).art(timeout=30)


def _send_image_binary(a, data, matte, timeout=15):
    if a.connection is None:
        a.open()
    upload_id = str(uuid.uuid4())
    inner = {'request': 'send_image', 'file_type': 'JPEG', 'matte_id': matte, 'id': upload_id}
    outer = {'method': 'ms.channel.emit', 'params': {'data': json.dumps(inner), 'to': 'host', 'event': 'art_app_request'}}
    header = json.dumps(outer, separators=(',', ':')).encode()
    a.connection.send_binary(len(header).to_bytes(2, 'big') + header + data)
    # Newer Frames (2018+) ignore this upload but keep sending other events, so the
    # library's wait_for_response would wait forever. Give up after `timeout` seconds.
    deadline = time.time() + timeout
    a.connection.settimeout(timeout)
    while time.time() < deadline:
        msg = a.get_websocket_message()
        if msg.get('request_id', msg.get('id')) != upload_id:
            continue
        if msg.get('event') == 'error':
            raise RuntimeError(f'send_image failed: {msg}')
        if msg.get('event') == 'image_added':
            return msg['content_id']
    raise TimeoutError('no image_added reply (newer Frame?)')


def send_image(a, data, matte='none'):
    try:
        return a.upload(data, file_type='JPEG', matte=matte, portrait_matte=matte)
    except Exception:  # noqa: BLE001  2017 firmware: one binary frame
        a.close()
        return _send_image_binary(a, data, matte)


def _thumbnail_binary(a, content_id, timeout=15):
    if a.connection is None:
        a.open()
    req_id = str(uuid.uuid4())
    inner = {'request': 'get_thumbnail', 'content_id': content_id, 'id': req_id,
             'conn_info': {'d2d_mode': 'socket', 'connection_id': uuid.uuid4().int % (4 << 30), 'id': req_id}}
    a.connection.send(json.dumps({'method': 'ms.channel.emit',
                                  'params': {'event': 'art_app_request', 'to': 'host', 'data': json.dumps(inner)}}))
    a.connection.settimeout(timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = a.connection.recv()
        if isinstance(raw, str):
            if '"error"' in raw and req_id in raw:
                raise RuntimeError(f'get_thumbnail {content_id} failed')
            continue
        for magic in (b'\xff\xd8\xff', b'\x89PNG'):
            pos = raw.find(magic)
            if pos >= 0:
                return raw[pos:]
    raise RuntimeError(f'no thumbnail received for {content_id}')


def get_thumbnail(a, content_id):
    try:
        data = a.get_thumbnail(content_id)
        if isinstance(data, dict):
            data = next(iter(data.values()))
        if isinstance(data, list):
            data = data[0]
        return bytes(data)
    except Exception:  # noqa: BLE001  2017 firmware: the thumbnail arrives on the websocket
        a.close()
        return _thumbnail_binary(a, content_id)


def status(ip):
    def work(opened):
        a = connect(ip)
        opened.append(a)
        try:
            return {'art_mode': a.get_artmode(), 'current': (a.get_current() or {}).get('content_id')}
        finally:
            a.close()
    return _bounded(work, [])


def diagnose(ip):
    """Step by step, to see where a connection fails."""
    steps = []
    tv = _tv(ip, 120)
    events.clear()

    def art(opened):
        a = tv.art(timeout=30)
        opened.append(a)
        try:
            return a.get_artmode()
        finally:
            a.close()

    # the remote step may wait up to two minutes for someone to press Allow on the TV
    for name, limit, fn in (('rest', 30, lambda opened: tv.rest_device_info()['device'].get('PowerState', 'ok')),
                            ('remote', 150, lambda opened: (opened.append(tv), tv.open(), tv.close(), 'ok')[3]),
                            ('art', LIMIT, art)):
        t = time.time()
        try:
            steps.append((name, 'ok', str(_bounded(fn, [], limit))[:80], round(time.time() - t, 1)))
        except Exception as e:  # noqa: BLE001
            steps.append((name, 'FAIL', f'{type(e).__name__}: {e}'[:200], round(time.time() - t, 1)))
            break
    return steps


def _local_networks():
    """The /24 networks this machine is on (inside Docker this needs host networking)."""
    nets = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('192.0.2.1', 9))  # no packet is sent; this just picks the outgoing interface
        nets.add(ipaddress.ip_network(s.getsockname()[0] + '/24', strict=False))
        s.close()
    except OSError:
        pass
    return [n for n in nets if n.is_private]


def _probe(ip):
    try:
        info = requests.get(f'http://{ip}:8001/api/v2/', timeout=1.5).json().get('device', {})
    except Exception:  # noqa: BLE001
        return None
    if info.get('FrameTVSupport') != 'true':
        return None
    return {'ip': str(ip), 'name': info.get('name', '').replace('[TV]', '').strip() or 'Frame',
            'model': info.get('modelName', '')}


def discover():
    """Find Frames on the local network by asking every address for Samsung's device info."""
    hosts = [ip for net in _local_networks() for ip in net.hosts()]
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        return [r for r in pool.map(_probe, hosts) if r]
