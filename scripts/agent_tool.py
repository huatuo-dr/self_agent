#!/usr/bin/env python3
"""Task-local client: no business credentials, environment discovery, or shell."""
import json
from pathlib import Path
import socket
import sys

MAX_REQUEST = 65536
MAX_RESPONSE = 2_000_000


def main():
    try:
        if len(sys.argv) != 3:
            raise ValueError('Usage: tool_client.py OP JSON')
        params = json.loads(sys.argv[2])
        if not isinstance(params, dict):
            raise ValueError('JSON must be an object')
        data = (json.dumps({'operation': sys.argv[1], 'params': params}) + '\n').encode()
        if len(data) > MAX_REQUEST:
            raise ValueError('Request is too large')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(150)
            connection.connect(str(Path(__file__).resolve().parent / 'agent-tools.sock'))
            connection.sendall(data)
            response = bytearray()
            while not response.endswith(b'\n'):
                chunk = connection.recv(min(65536, MAX_RESPONSE + 1 - len(response)))
                if not chunk or len(response) + len(chunk) > MAX_RESPONSE:
                    raise ValueError('Invalid response')
                response.extend(chunk)
        result = json.loads(response)
    except (OSError, ValueError) as error:
        result = {'ok': False, 'error': {'type': 'client_error'}}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
