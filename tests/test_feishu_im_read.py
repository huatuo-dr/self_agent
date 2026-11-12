import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from assistant import feishu_im_read


class FeishuIMReadTests(unittest.TestCase):
    def test_only_user_identity_reads_are_constructed(self):
        args = feishu_im_read.command_for(
            {'operation': 'messages', 'chat_id': 'oc_test', 'start': '2026-10-16',
             'end': '2026-10-17', 'page_size': 50}, 'lark-cli')
        self.assertIn('--as', args)
        self.assertEqual(args[args.index('--as') + 1], 'user')
        self.assertEqual(args[1:4], ['api', 'GET', '/open-apis/im/v1/messages'])
        params = json.loads(args[args.index('--params') + 1])
        self.assertEqual(params['container_id'], 'oc_test')
        self.assertEqual(params['sort_type'], 'ByCreateTimeAsc')
        self.assertEqual(params['card_msg_content_type'], 'user_card_content')
        thread_args = feishu_im_read.command_for(
            {'operation': 'threads', 'thread_id': 'omt_test'}, 'lark-cli')
        thread_params = json.loads(thread_args[thread_args.index('--params') + 1])
        self.assertEqual(thread_params['container_id_type'], 'thread')
        self.assertNotIn('start_time', thread_params)
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            feishu_im_read.command_for({'operation': 'send', 'chat_id': 'oc_test'}, 'lark-cli')
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            feishu_im_read.command_for({'operation': 'messages', 'chat_id': 'oc_test',
                                        'file': '/tmp/token'}, 'lark-cli')

    def test_gateway_checks_bound_identity_and_reports_missing_scope(self):
        calls = []
        def fake_run(args, **_):
            calls.append(args)
            if args[1:3] == ['auth', 'status']:
                return subprocess.CompletedProcess(args, 0, json.dumps({
                    'identities': {'user': {'openId': 'ou_owner',
                                            'scope': ['im:chat:read']}}}), '')
            return subprocess.CompletedProcess(args, 3, '', json.dumps({
                'ok': False, 'error': {'type': 'authorization', 'subtype': 'missing_scope',
                                      'missing_scopes': ['im:chat:read'],
                                      'hint': 'do not return this string'}}))
        with patch.object(feishu_im_read.subprocess, 'run', fake_run):
            mismatch = feishu_im_read.execute({'operation': 'search', 'query': '工程'},
                                               'lark-cli', 'ou_other')
            self.assertEqual(mismatch['error']['type'], 'owner_identity_mismatch')
            self.assertEqual(len(calls), 1)
            result = feishu_im_read.execute({'operation': 'search', 'query': '工程'},
                                             'lark-cli', 'ou_owner')
        self.assertEqual(result['error']['missing_scopes'], ['im:chat:read'])
        self.assertNotIn('hint', result['error'])
        self.assertEqual(calls[-1][1:3], ['im', '+chat-search'])

    def test_socket_is_private_and_serves_bounded_request(self):
        async def check(path):
            with patch.object(feishu_im_read, 'execute', return_value={
                    'ok': True, 'data': {'chats': []}}):
                task = asyncio.create_task(feishu_im_read.serve(path, 'lark-cli', 'ou_owner'))
                try:
                    for _ in range(100):
                        if path.exists():
                            break
                        if task.done():
                            error = task.exception()
                            if isinstance(error, PermissionError):
                                self.skipTest('当前沙箱不允许绑定 Unix socket')
                            if error:
                                raise error
                        await asyncio.sleep(0.01)
                    self.assertTrue(path.exists())
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    reader, writer = await asyncio.open_unix_connection(str(path))
                    writer.write(b'{"operation":"search","query":"test"}\n')
                    await writer.drain()
                    result = json.loads(await reader.readline())
                    self.assertTrue(result['ok'])
                    writer.close()
                    await writer.wait_closed()
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self.assertFalse(path.exists())
        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(check(Path(directory) / 'read.sock'))
