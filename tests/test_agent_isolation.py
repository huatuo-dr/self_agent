import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from assistant.agent_policy import policy_for
from assistant.agent_isolation import isolated, IsolationError


@unittest.skipUnless(shutil.which('bwrap'), 'bubblewrap unavailable')
class AgentIsolationTests(unittest.TestCase):
    def test_linked_sibling_reference_is_staged_and_used_by_isolated_skill_script(self):
        import hashlib
        from assistant.skills import task_materials
        with tempfile.TemporaryDirectory(prefix='skill-source-') as directory:
            source = Path(directory)
            skill = source / 'skills/report'
            shared = source / 'skills/shared/value.txt'
            skill.mkdir(parents=True)
            shared.parent.mkdir(parents=True)
            shared.write_text('linked fixture')
            (skill / 'SKILL.md').write_text('Read [reference](../shared/value.txt) and run build.py.')
            (skill / 'build.py').write_text('#!/usr/bin/python3\nfrom pathlib import Path\n'
                'p=Path(__file__).parent\nPath("linked.txt").write_text((p/"../shared/value.txt").read_text())\n')
            (skill / 'build.py').chmod(0o500)
            item = {'name': 'report', 'description': 'report', 'path': 'skills/report/SKILL.md'}
            data = {'snapshot': str(source), 'index': [item], 'files': {
                p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in source.rglob('*') if p.is_file()}}
            with task_materials(data, [item]) as staged:
                policy = policy_for(self.cfg, 'chat', self.root, read_only_dirs=(staged,))
                with isolated(policy, self.cfg, self.work, sys.executable) as box:
                    path = box.map_path(staged / 'skills/report/build.py')
                    code = ('import json,pathlib,subprocess; '
                            f'subprocess.run([{str(path)!r}],check=True); '
                            'print(json.dumps(pathlib.Path("linked.txt").read_text()))')
                    self.assertEqual(self.run_code(box, code), 'linked fixture')

    def test_skill_input_preserves_references_and_executes_without_becoming_writable(self):
        with tempfile.TemporaryDirectory(prefix='skill-materials-') as directory:
            materials = Path(directory)
            skill = materials / 'skills/report'
            (skill / 'references').mkdir(parents=True)
            (skill / 'references/value.txt').write_text('from reference')
            (skill / 'build.py').write_text(
                '#!/usr/bin/python3\n'
                'from pathlib import Path\n'
                'value=(Path(__file__).parent/"references/value.txt").read_text()\n'
                'Path("from-skill.txt").write_text(value)\n')
            (skill / 'build.py').chmod(0o500)
            policy = policy_for(self.cfg, 'chat', self.root, read_only_dirs=(materials,))
            with isolated(policy, self.cfg, self.work, sys.executable) as box:
                mapped = box.map_path(skill)
                code = f'''import json,pathlib,subprocess,sys
p=pathlib.Path({str(mapped)!r})
subprocess.run([str(p/'build.py')],check=True)
try:
 (p/'references/value.txt').write_text('changed')
 writable=True
except OSError:
 writable=False
print(json.dumps([pathlib.Path('from-skill.txt').read_text(),writable]))'''
                self.assertEqual(self.run_code(box, code), ['from reference', False])
            self.assertEqual((skill / 'references/value.txt').read_text(), 'from reference')

    def test_skill_input_does_not_allow_hidden_secrets_or_linked_host_files(self):
        with tempfile.TemporaryDirectory(prefix='skill-materials-') as directory:
            materials = Path(directory)
            policy = policy_for(self.cfg, 'chat', self.root, read_only_dirs=(materials,))
            for filename in ('.env', 'linked'):
                path = materials / filename
                if filename == '.env':
                    path.write_text('sensitive fixture')
                else:
                    path.symlink_to(self.root / '.env')
                with self.subTest(filename=filename), self.assertRaises(IsolationError):
                    with isolated(policy, self.cfg, self.work, sys.executable):
                        pass
                path.unlink()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='opt-equivalent-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'runtime'
        self.root.mkdir()
        (self.root / 'source.py').write_text('before\n')
        for name in ('.env', '.local/state.db', '.project/credential/key'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('sensitive-fixture')
        self.work = Path(self.tmp.name) / 'work'
        self.work.mkdir()
        self.cfg = {'AGENT_AUTH_FILES': {'codex': {}}}

    def run_code(self, box, code):
        result = subprocess.run(box.wrap([sys.executable, '-c', code]),
                                cwd=self.work, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_discussion_source_is_readonly_and_sensitive_host_data_hidden(self):
        external = Path(self.tmp.name) / 'outside-secret'
        external.write_text('outside')
        (self.root / 'escape').symlink_to(external)
        policy = policy_for(self.cfg, 'develop_discuss', self.root)
        with isolated(policy, self.cfg, self.work, sys.executable) as box:
            source = box.map_path(self.root)
            code = f'''import json, pathlib, os
root=pathlib.Path({str(source)!r})
try:
 (root/'source.py').write_text('bad')
 writable=True
except OSError:
 writable=False
print(json.dumps(dict(source=(root/'source.py').read_text(), writable=writable,
 hidden=[(root/p).exists() for p in ['.env','.local/state.db','.project/credential/key','escape']],
 host=pathlib.Path({str(external)!r}).exists(), env='HOST_PRIVATE_TOKEN' in os.environ)))'''
            with patch.dict(os.environ, {'HOST_PRIVATE_TOKEN': 'fixture-secret'}):
                data = self.run_code(box, code)
            self.assertEqual(data, {'source': 'before\n', 'writable': False,
                                    'hidden': [False] * 4, 'host': False, 'env': False})

    def test_approved_source_changes_publish_without_touching_sensitive_files(self):
        policy = policy_for(self.cfg, 'develop_execute', self.root, approval_ref='db-task-1')
        with isolated(policy, self.cfg, self.work, sys.executable) as box:
            source = box.map_path(self.root)
            self.run_code(box, f'import pathlib,json; pathlib.Path({str(source / "source.py")!r}).write_text("after\\n"); print(json.dumps(True))')
            self.assertEqual((self.root / 'source.py').read_text(), 'before\n')
            box.publish()
        self.assertEqual((self.root / 'source.py').read_text(), 'after\n')
        self.assertEqual((self.root / '.env').read_text(), 'sensitive-fixture')

    def test_publish_rejects_symlinks_and_concurrent_source_changes(self):
        policy = policy_for(self.cfg, 'develop_execute', self.root, approval_ref='db-task-2')
        with isolated(policy, self.cfg, self.work, sys.executable) as box:
            source = box.map_path(self.root)
            self.run_code(box, f'import pathlib,json; pathlib.Path({str(source / "new")!r}).symlink_to("/etc/passwd"); print(json.dumps(True))')
            with self.assertRaises(IsolationError):
                box.publish()
        with isolated(policy, self.cfg, self.work, sys.executable) as box:
            (self.root / 'source.py').write_text('concurrent')
            with self.assertRaises(IsolationError):
                box.publish()

    def test_report_work_cwd_and_scoped_repository(self):
        work = self.root / '.project/reports/task-1'
        work.mkdir(parents=True)
        repo = self.root / '.project/repo'
        repo.mkdir()
        (repo / 'code.txt').write_text('repo-fixture')
        (repo / '.env').write_text('fixture-key')
        cfg = self.cfg
        policy = policy_for(cfg, 'report', self.root, writable_roots=(repo,))
        with isolated(policy, cfg, work, sys.executable) as box:
            mapped = box.map_path(repo)
            data = self.run_code(box, f'import os,json,pathlib; print(json.dumps([os.getcwd(), pathlib.Path({str(mapped / "code.txt")!r}).read_text(), pathlib.Path({str(mapped / ".env")!r}).exists()]))')
            self.assertIn('/.project/reports/task-1', data[0])
            self.assertEqual(data[1:], ['repo-fixture', False])
        with self.assertRaises(IsolationError):
            with isolated(policy, cfg, self.work, sys.executable):
                pass

    def test_network_and_child_processes_use_allowlisted_proxy_only(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'model-fixture')
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        port = server.server_port
        cfg = {**self.cfg, 'OPENAI_BASE_URL': f'http://127.0.0.1:{port}'}
        policy = policy_for(cfg, 'chat', self.root)
        with isolated(policy, cfg, self.work, sys.executable) as box:
            code = f'''import json, urllib.request, urllib.error, socket, subprocess,sys,os
proxy=urllib.request.ProxyHandler({{'http':os.environ['HTTP_PROXY']}})
opener=urllib.request.build_opener(proxy)
# urllib's environment-based localhost bypass must be disabled explicitly.
import urllib.request as u
u.proxy_bypass=lambda host: False
allowed=opener.open('http://127.0.0.1:{port}/',timeout=3).read().decode()
try:
 opener.open('http://127.0.0.1:{port+1}/',timeout=3)
 denied=False
except urllib.error.HTTPError as exc:
 denied=exc.code==403
child=subprocess.check_output([sys.executable,'-c',"import socket; s=socket.socket(); s.settimeout(1); print(s.connect_ex(('1.1.1.1',443)))"],text=True).strip()
tunnel=socket.create_connection(('127.0.0.1',18080),timeout=3)
tunnel.sendall(b'CONNECT 127.0.0.1:{port} HTTP/1.1\\r\\nHost: 127.0.0.1:{port}\\r\\n\\r\\n')
header=tunnel.recv(4096)
tunnel.sendall(b'GET / HTTP/1.1\\r\\nHost: 127.0.0.1\\r\\nConnection: close\\r\\n\\r\\n')
response=b''
while True:
 block=tunnel.recv(4096)
 if not block: break
 response+=block
tunnel.close()
print(json.dumps([allowed,denied,int(child)!=0,b'200 Connection Established' in header and b'model-fixture' in response]))'''
            self.assertEqual(self.run_code(box, code), ['model-fixture', True, True, True])

    def test_only_selected_auth_file_and_api_environment_are_visible(self):
        auth = Path(self.tmp.name) / 'auth-fixture.json'
        auth.write_text('selected-auth-fixture')
        cfg = {'AGENT_BACKEND': 'codex',
               'AGENT_AUTH_FILES': {'codex': {'.codex/auth.json': str(auth)},
                                    'grok': {'unused': '/nonexistent'}},
               'OPENAI_API_KEY': 'selected-key-fixture',
               'ANTHROPIC_API_KEY': 'unselected-key-fixture'}
        input_file = Path(self.tmp.name) / 'input.txt'
        input_file.write_text('input-fixture')
        policy = policy_for(cfg, 'chat', self.root, input_files=(input_file,))
        with isolated(policy, cfg, self.work, sys.executable) as box:
            mapped = box.map_path(input_file)
            data = self.run_code(box, f"import pathlib,json,os; print(json.dumps([pathlib.Path.home().joinpath('.codex/auth.json').read_text(), os.environ.get('OPENAI_API_KEY'), 'ANTHROPIC_API_KEY' in os.environ, pathlib.Path({str(mapped)!r}).read_text()]))")
            self.assertEqual(data, ['selected-auth-fixture', 'selected-key-fixture', False, 'input-fixture'])

    def test_auth_targets_cannot_load_unreviewed_cli_configuration(self):
        auth = Path(self.tmp.name) / 'fake-config'
        auth.write_text('fixture')
        cfg = {'AGENT_AUTH_FILES': {'codex': {'.codex/config.toml': str(auth)}}}
        with isolated(policy_for(cfg, 'chat', self.root), cfg, self.work, sys.executable) as box:
            with self.assertRaises(IsolationError):
                box.wrap([sys.executable, '-c', 'pass'])

    def test_missing_bubblewrap_has_no_native_fallback(self):
        with patch('assistant.agent_isolation.shutil.which', return_value=None):
            with self.assertRaises(IsolationError):
                with isolated(policy_for(self.cfg, 'chat', self.root), self.cfg,
                              self.work, sys.executable):
                    pass

    def test_work_cannot_rebind_project_or_non_task_user_data(self):
        policy = policy_for(self.cfg, 'chat', self.root)
        for work in (self.root / '.project', self.root.parent, Path('/tmp')):
            with self.subTest(work=work), self.assertRaises(IsolationError):
                with isolated(policy, self.cfg, work, sys.executable):
                    pass

    def test_grok_subscription_auth_is_minimal_and_isolated(self):
        home = Path(self.tmp.name) / 'grok-host-home'
        home.mkdir()
        (home / 'auth.json').write_text('grok-auth-fixture')
        (home / 'config.json').write_text('unnecessary-config-fixture')
        cfg = {'AGENT_BACKEND': 'grok', 'GROK_HOME': str(home)}
        policy = policy_for(cfg, 'chat', self.root)
        self.assertIn(('cli-chat-proxy.grok.com', 443), policy.network_allowlist)
        with isolated(policy, cfg, self.work, sys.executable) as box:
            data = self.run_code(box, "import pathlib,os,json; p=pathlib.Path(os.environ['GROK_HOME']); print(json.dumps([p.joinpath('auth.json').read_text(), p.joinpath('config.json').exists()]))")
            self.assertEqual(data, ['grok-auth-fixture', False])

    def test_hardlinks_do_not_expose_excluded_sensitive_inodes(self):
        os.link(self.root / '.env', self.root / 'innocent-name')
        policy = policy_for(self.cfg, 'develop_discuss', self.root)
        with isolated(policy, self.cfg, self.work, sys.executable) as box:
            mapped = box.map_path(self.root / 'innocent-name')
            self.assertFalse(self.run_code(box, f'import pathlib,json; print(json.dumps(pathlib.Path({str(mapped)!r}).exists()))'))

    def test_task_work_rejects_hidden_hardlinks_symlinks_and_fifos(self):
        policy = policy_for(self.cfg, 'chat', self.root)
        for kind in ('hardlink', 'symlink', 'directory_symlink', 'fifo'):
            innocent = self.work / 'innocent-name'
            if kind == 'hardlink':
                os.link(self.root / '.env', innocent)
            elif kind == 'symlink':
                innocent.symlink_to(self.root / '.env')
            elif kind == 'directory_symlink':
                innocent.symlink_to(self.root / '.local', target_is_directory=True)
            else:
                os.mkfifo(innocent)
            try:
                with self.subTest(kind=kind), self.assertRaises(IsolationError):
                    with isolated(policy, self.cfg, self.work, sys.executable) as box:
                        self.run_code(box, 'import pathlib,json; p=pathlib.Path("innocent-name"); p.write_text("compromised"); print(json.dumps(True))')
            finally:
                innocent.unlink()
            self.assertEqual((self.root / '.env').read_text(), 'sensitive-fixture')

    def test_wrap_revalidates_work_after_gateway_preparation(self):
        policy = policy_for(self.cfg, 'chat', self.root)
        with isolated(policy, self.cfg, self.work, sys.executable) as box:
            os.link(self.root / '.env', self.work / 'late-input')
            with self.assertRaises(IsolationError):
                box.wrap([sys.executable, '-c', 'pass'])

    def test_task_owned_gateway_socket_remains_usable(self):
        import socket
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(self.work / 'gateway.sock'))
        server.listen(1)
        def answer():
            client, _ = server.accept()
            with client:
                client.sendall(b'gateway-fixture')
        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        with isolated(policy_for(self.cfg, 'chat', self.root), self.cfg,
                      self.work, sys.executable) as box:
            data = self.run_code(box, 'import socket,json; s=socket.socket(socket.AF_UNIX); s.connect("gateway.sock"); print(json.dumps(s.recv(100).decode()))')
            self.assertEqual(data, 'gateway-fixture')
        thread.join(timeout=2)

    def test_npm_runtime_rejects_protected_siblings_and_links(self):
        package = Path(self.tmp.name) / 'npm/node_modules/fixture-agent'
        package.mkdir(parents=True)
        executable = package / 'agent.py'
        executable.write_text('#!/usr/bin/python3\nimport json; print(json.dumps(True))\n')
        executable.chmod(0o755)
        for name, link in (('.env', False), ('.local', False), ('innocent', True)):
            path = package / name
            if link:
                path.symlink_to(self.root / '.env')
            elif name == '.local':
                path.mkdir()
                (path / 'data').write_text('runtime-sensitive-fixture')
            else:
                path.write_text('runtime-sensitive-fixture')
            try:
                with self.subTest(name=name), self.assertRaises(IsolationError):
                    with isolated(policy_for(self.cfg, 'chat', self.root), self.cfg,
                                  self.work, executable) as box:
                        self.run_code(box, 'import json; print(json.dumps(True))')
            finally:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()

    def test_project_and_home_inside_system_runtime_are_masked(self):
        with tempfile.TemporaryDirectory(prefix='agent-system-runtime-fixture-', dir=Path.home()) as tmp:
            root = Path(tmp) / 'project'
            root.mkdir()
            (root / '.env').write_text('system-runtime-secret-fixture')
            home = Path(tmp) / 'home'
            home.mkdir()
            (home / 'private-data').write_text('user-private-fixture')
            with patch.dict(os.environ, {'HOME': str(home)}), patch('assistant.agent_isolation.SYSTEM_RUNTIME_ROOTS', ('/usr', tmp)):
                policy = policy_for(self.cfg, 'chat', root)
                with isolated(policy, self.cfg, self.work, sys.executable) as box:
                    code = f'import pathlib,json; print(json.dumps([pathlib.Path({str(root / ".env")!r}).exists(), pathlib.Path({str(home / "private-data")!r}).exists()]))'
                    command = box.wrap([sys.executable, '-c', code])
                    unmasked = []
                    index = 0
                    while index < len(command):
                        if command[index] == '--tmpfs' and command[index + 1] in (str(root), str(home)):
                            index += 2
                            continue
                        unmasked.append(command[index])
                        index += 1
                    control = subprocess.run(unmasked, text=True, capture_output=True, timeout=15)
                    self.assertEqual(control.returncode, 0, control.stderr)
                    self.assertEqual(json.loads(control.stdout), [True, True])
                    self.assertEqual(self.run_code(box, code), [False, False])

    def test_resource_limits_apply_to_children(self):
        with isolated(policy_for(self.cfg, 'chat', self.root), self.cfg,
                      self.work, sys.executable) as box:
            data = self.run_code(box, 'import resource,json,subprocess,sys; print(subprocess.check_output([sys.executable,"-c","import resource,json; print(json.dumps([resource.getrlimit(resource.RLIMIT_NOFILE)[1],resource.getrlimit(resource.RLIMIT_NPROC)[1],resource.getrlimit(resource.RLIMIT_FSIZE)[1],resource.getrlimit(resource.RLIMIT_CORE)[1]]))"], text=True))')
            self.assertLessEqual(data[0], 1024)
            self.assertGreater(data[0], 0)
            self.assertLessEqual(data[1], 512)
            self.assertGreater(data[1], 0)
            self.assertLessEqual(data[2], 256 * 1024 * 1024)
            self.assertGreater(data[2], 0)
            self.assertEqual(data[3], 0)

    def test_npm_internal_binary_hardlinks_are_allowed_but_external_ones_are_rejected(self):
        package = Path(self.tmp.name) / 'npm/node_modules/fixture-native-agent'
        package.mkdir(parents=True)
        executable = package / 'agent'
        executable.write_text('#!/usr/bin/python3\nimport json; print(json.dumps(True))\n')
        executable.chmod(0o755)
        os.link(executable, package / 'internal-binary')
        with isolated(policy_for(self.cfg, 'chat', self.root), self.cfg,
                      self.work, executable) as box:
            result = subprocess.run(box.wrap([str(executable)]), text=True,
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout))
        os.link(self.root / '.env', package / 'external-hardlink')
        with isolated(policy_for(self.cfg, 'chat', self.root), self.cfg,
                      self.work, executable) as box:
            with self.assertRaises(IsolationError):
                box.wrap([str(executable)])

    def test_custom_owner_profile_hidden_from_source_and_rejected_on_publish(self):
        profile = self.root / 'identity.json'
        profile.write_text('owner-private-fixture')
        cfg = {**self.cfg, 'OWNER_PROFILE_PATH': 'identity.json'}
        policy = policy_for(cfg, 'develop_execute', self.root, approval_ref='db-approved')
        with isolated(policy, cfg, self.work, sys.executable) as box:
            mapped = box.map_path(profile)
            self.assertFalse(self.run_code(box, f'import pathlib,json; print(json.dumps(pathlib.Path({str(mapped)!r}).exists()))'))
            self.run_code(box, f'import pathlib,json; pathlib.Path({str(mapped)!r}).write_text("replacement"); print(json.dumps(True))')
            with self.assertRaises(IsolationError):
                box.publish()
        self.assertEqual(profile.read_text(), 'owner-private-fixture')

    def test_external_owner_profile_inside_system_bind_is_unreadable_and_unwritable(self):
        with tempfile.TemporaryDirectory(prefix='agent-profile-fixture-', dir=Path.home()) as tmp:
            external = Path(tmp) / 'identity.json'
            external.write_text('external-owner-private-fixture')
            home = Path(tmp) / 'home'
            home.mkdir()
            cfg = {**self.cfg, 'OWNER_PROFILE_PATH': str(external)}
            with patch.dict(os.environ, {'HOME': str(home)}), patch('assistant.agent_isolation.SYSTEM_RUNTIME_ROOTS', ('/usr', tmp)):
                policy = policy_for(cfg, 'chat', self.root)
                with isolated(policy, cfg, self.work, sys.executable) as box:
                    code = f'''import pathlib,json
p=pathlib.Path({str(external)!r})
blocked=[]
for operation in (lambda:p.read_text(),lambda:p.write_text('compromised')):
 try:
  operation()
  blocked.append(False)
 except OSError:
  blocked.append(True)
print(json.dumps(blocked))'''
                    self.assertEqual(self.run_code(box, code), [True, True])
            self.assertEqual(external.read_text(), 'external-owner-private-fixture')

    def test_host_proxy_bounds_simultaneous_connections(self):
        import select
        import socket
        import time
        from assistant.agent_network import NetworkProxy
        path = Path(self.tmp.name) / 'bound-proxy.sock'
        clients = []
        with NetworkProxy(path, ()) as proxy:
            processed = threading.Event()
            process_request = proxy.server.process_request

            def observe_request(request, address):
                try:
                    process_request(request, address)
                finally:
                    processed.set()

            # Measure active handlers, independently of the kernel's listen
            # backlog: wait for acceptance/rejection before the next connection.
            with patch.object(proxy.server, 'process_request', observe_request):
                try:
                    for index in range(40):
                        processed.clear()
                        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        clients.append(client)
                        client.settimeout(2)
                        client.connect(str(path))
                        # Incomplete headers keep accepted handlers occupied.
                        try:
                            client.sendall(b'GET ')
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                        self.assertTrue(processed.wait(2), 'proxy did not process connection')
                    deadline = time.monotonic() + 3
                    closed = set()
                    while time.monotonic() < deadline and len(closed) < 8:
                        pending = [s for s in clients if s not in closed]
                        ready, _, _ = select.select(pending, (), (), 0.05)
                        for client in ready:
                            try:
                                if not client.recv(1):
                                    closed.add(client)
                            except ConnectionResetError:
                                closed.add(client)
                    self.assertEqual(closed, set(clients[32:]))
                finally:
                    for client in clients:
                        client.close()

    def test_uninspectable_work_directory_cannot_hide_external_hardlink(self):
        hidden = self.work / 'ordinary-directory'
        hidden.mkdir()
        os.link(self.root / '.env', hidden / 'ordinary-file')
        hidden.chmod(0o000)
        try:
            with self.assertRaises(IsolationError):
                with isolated(policy_for(self.cfg, 'chat', self.root), self.cfg,
                              self.work, sys.executable) as box:
                    self.run_code(box, 'import pathlib,json; p=pathlib.Path("ordinary-directory"); p.chmod(0o700); p.joinpath("ordinary-file").write_text("compromised"); print(json.dumps(True))')
        finally:
            hidden.chmod(0o700)
        self.assertEqual((self.root / '.env').read_text(), 'sensitive-fixture')

    def test_source_publication_preserves_safe_mode_changes(self):
        script = self.root / 'run.sh'
        script.write_text('#!/bin/sh\nexit 0\n')
        script.chmod(0o644)
        policy = policy_for(self.cfg, 'develop_execute', self.root, approval_ref='db-approved')
        with isolated(policy, self.cfg, self.work, sys.executable) as box:
            mapped = box.map_path(script)
            self.run_code(box, f'import pathlib,json; pathlib.Path({str(mapped)!r}).chmod(0o4755); print(json.dumps(True))')
            box.publish()
        self.assertEqual(script.stat().st_mode & 0o7777, 0o755)
        self.assertEqual(script.read_text(), '#!/bin/sh\nexit 0\n')

    def test_source_publication_detects_concurrent_permission_change(self):
        source = self.root / 'source.py'
        source.chmod(0o644)
        policy = policy_for(self.cfg, 'develop_execute', self.root, approval_ref='db-approved')
        with isolated(policy, self.cfg, self.work, sys.executable) as box:
            source.chmod(0o600)
            with self.assertRaises(IsolationError):
                box.publish()
        self.assertEqual(source.stat().st_mode & 0o7777, 0o600)

    def test_selected_claude_oauth_and_native_model_env_are_supplied(self):
        names = ('CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_MODEL',
                 'ANTHROPIC_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
                 'ANTHROPIC_DEFAULT_OPUS_MODEL')
        cfg = {'AGENT_BACKEND': 'claudecode', 'AGENT_AUTH_FILES': {'claudecode': {}},
               **{name: 'fixture-' + name for name in names},
               'OPENAI_API_KEY': 'unselected-fixture'}
        with isolated(policy_for(cfg, 'chat', self.root), cfg, self.work,
                      sys.executable) as box:
            data = self.run_code(box, f'import os,json; print(json.dumps([[os.environ.get(n) for n in {names!r}], "OPENAI_API_KEY" in os.environ]))')
            self.assertEqual(data, [['fixture-' + name for name in names], False])
