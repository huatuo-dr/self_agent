"""Host-side exact-host HTTP/CONNECT proxy; no service credentials are exposed."""
import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit


def bridge(left, right):
    """Relay an established TCP stream; destination was checked before connect."""
    try:
        while True:
            ready, _, _ = select.select((left, right), (), (), 60)
            if not ready:
                return
            for source in ready:
                data = source.recv(65536)
                if not data:
                    return
                (right if source is left else left).sendall(data)
    except OSError:
        return


class _Proxy(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 32

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        try:
            header = bytearray()
            while b'\r\n\r\n' not in header and len(header) <= 65536:
                block = self.request.recv(4096)
                if not block:
                    return
                header.extend(block)
            if len(header) > 65536 or b'\r\n\r\n' not in header:
                return
            head, extra = bytes(header).split(b'\r\n\r\n', 1)
            first, *headers = head.split(b'\r\n')
            method, target, version = first.decode('ascii').split(' ')
            if method == 'CONNECT':
                parsed = urlsplit('https://' + target)
                port = parsed.port or 443
                rewritten = None
            else:
                parsed = urlsplit(target)
                if parsed.scheme != 'http' or parsed.username or parsed.password:
                    raise ValueError()
                port = parsed.port or 80
                path = parsed.path or '/'
                if parsed.query:
                    path += '?' + parsed.query
                clean = [h for h in headers if h.split(b':', 1)[0].lower() not in
                         (b'proxy-authorization', b'proxy-connection', b'connection', b'host')]
                authority = parsed.hostname
                if ':' in authority:
                    authority = '[' + authority + ']'
                clean += [('Host: ' + authority + ':' + str(port)).encode(), b'Connection: close']
                rewritten = f'{method} {path} {version}'.encode() + b'\r\n' + b'\r\n'.join(clean) + b'\r\n\r\n'
            host = (parsed.hostname or '').lower().rstrip('.')
            if (host, port) not in self.server.allowlist:
                self.request.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
                return
            with socket.create_connection((host, port), timeout=10) as upstream:
                if rewritten is None:
                    self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                else:
                    upstream.sendall(rewritten)
                if extra:
                    upstream.sendall(extra)
                bridge(self.request, upstream)
        except (OSError, ValueError, UnicodeError):
            try:
                self.request.sendall(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
            except OSError:
                pass


class NetworkProxy:
    def __init__(self, path, allowlist):
        self.server = _Proxy(str(path), _Handler)
        self.server.allowlist = frozenset(allowlist)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.05}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
