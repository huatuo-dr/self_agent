"""Standalone loopback-to-UNIX relay, executed inside the network namespace."""
import resource
import select
import socket
import socketserver
import subprocess
import sys
import threading


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.connect(self.server.unix_path)
            while True:
                ready, _, _ = select.select((self.request, upstream), (), (), 60)
                if not ready:
                    return
                for source in ready:
                    block = source.recv(65536)
                    if not block:
                        return
                    (upstream if source is self.request else self.request).sendall(block)
        except OSError:
            pass
        finally:
            upstream.close()


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
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


def limit_resources():
    # These hard limits are inherited by every model/tool descendant. NPROC is
    # Linux's per-real-UID limit (including threads), not a cgroup task quota.
    for kind, maximum in ((resource.RLIMIT_NOFILE, 1024),
                          (resource.RLIMIT_NPROC, 512),
                          (resource.RLIMIT_FSIZE, 256 * 1024 * 1024),
                          (resource.RLIMIT_CORE, 0)):
        _, inherited = resource.getrlimit(kind)
        hard = maximum if inherited == resource.RLIM_INFINITY else min(inherited, maximum)
        resource.setrlimit(kind, (hard, hard))


def main():
    limit_resources()
    unix_path, port, *command = sys.argv[1:]
    with Server(('127.0.0.1', int(port)), Relay) as server:
        server.unix_path = unix_path
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            return subprocess.call(command)
        finally:
            server.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
