import asyncio
import hashlib
import json
import os
import subprocess
import shutil
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

from assistant import agent_tools


class Sandbox:
    def __init__(self, work):
        self.work_path = str(work)
        self.path_map = {}
    def map_path(self, path):
        return str(path)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.calls = []
        async def run(args, timeout, input_text=None, cwd=None):
            self.calls.append((args, cwd))
            return '{}'
        self.run = run
        self.cfg = {'GITLAB_SKILL_ENABLED': True, 'GLAB_BIN': 'glab', 'LARK_CLI_BIN': 'lark',
                    'FEISHU_OWNER_CHAT_ID': 'oc_owner', 'FEISHU_OWNER_OPEN_ID': 'ou_owner',
                    '_delivery_root': 'om_thread'}
        self.scope = {'kind': 'chat', 'gitlab_mode': 'read', 'projects': [
            {'host': 'git.example', 'project': 'team/repo'}], 'repositories': True,
            'deliver_files': True}
        self.gateway = agent_tools.TaskTools(self.cfg, self.root, self.scope, self.run,
                                               self.work, Sandbox(self.work))

    async def test_read_scope_denies_writes_auth_file_flags_and_other_projects(self):
        for args in (['auth', 'token'], ['mr', 'close', '1'], ['mr', 'view', '1', '--web'],
                     ['api', 'projects/team%2Frepo/variables'],
                     ['mr', 'view', '1', '--body', '@/etc/passwd'],
                     ['mr', 'view', '1', '--repo', 'git.example/else/repo']):
            with self.subTest(args=args):
                result = await self.gateway.dispatch('gitlab_cli', {
                    'host': 'git.example', 'project': 'team/repo', 'args': args})
                self.assertFalse(result['ok'])
        self.assertEqual(self.calls, [])

    async def test_gitlab_read_has_explicit_scoped_repository(self):
        result = await self.gateway.dispatch('gitlab_cli', {'host': 'git.example',
            'project': 'team/repo', 'args': ['mr', 'view', '12', '--output', 'json']})
        self.assertTrue(result['ok'])
        self.assertIn('git.example/team/repo', self.calls[0][0])

    async def test_failure_never_exposes_stderr(self):
        async def fail(*args, **kwargs):
            raise RuntimeError('TOKEN=SECRET /home/private/.env')
        self.gateway.run = fail
        result = await self.gateway.dispatch('gitlab_cli', {'host': 'git.example',
            'project': 'team/repo', 'args': ['mr', 'list']})
        self.assertFalse(result['ok'])
        self.assertNotIn('SECRET', json.dumps(result))

    async def test_send_file_is_pinned_to_thread_and_rejects_symlinks(self):
        path = self.work / 'report.html'
        path.write_text('report')
        async def sent(args, timeout, input_text=None, cwd=None):
            self.calls.append((args, cwd))
            return '{"ok":true,"data":{"message_id":"om_sent"}}'
        self.gateway.run = sent
        result = await self.gateway.dispatch('send_file', {'path': str(path)})
        self.assertTrue(result['ok'])
        args, cwd = self.calls[0]
        self.assertIn('om_thread', args)
        self.assertIn('./report.html', args)
        self.assertNotEqual(cwd, path.parent)
        self.assertFalse(Path(cwd).exists())
        (self.work / 'link').symlink_to(path)
        self.assertFalse((await self.gateway.dispatch('send_file', {'path': str(self.work / 'link')}))['ok'])
        self.assertFalse((await self.gateway.dispatch('send_file', {'path': '/etc/passwd'}))['ok'])

    async def test_socket_lifecycle_and_client(self):
        factory = agent_tools.tool_gateway(self.cfg, self.root, self.scope, self.run)
        async with factory(self.work, Sandbox(self.work)) as instructions:
            self.assertIn('tool_client.py', instructions)
            socket = self.work / agent_tools.SOCKET_NAME
            self.assertEqual(socket.stat().st_mode & 0o777, 0o600)
            self.assertTrue((self.work / 'tool_client.py').is_file())
            client = await asyncio.create_subprocess_exec(sys.executable,
                str(self.work / 'tool_client.py'), 'gitlab_cli', json.dumps({
                    'host': 'git.example', 'project': 'team/repo', 'args': ['auth', 'status']}),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            output, error = await client.communicate()
            self.assertEqual(client.returncode, 0, error)
            self.assertTrue(json.loads(output)['ok'])
            reader, writer = await asyncio.open_unix_connection(str(socket))
            writer.write(b'{"operation":"history_search","params":{"query":"x"}}\n')
            await writer.drain()
            result = json.loads(await reader.readline())
            self.assertFalse(result['ok'])
            writer.close()
            await writer.wait_closed()
        self.assertFalse(socket.exists())

    async def test_scope_is_not_mutable_by_requests(self):
        result = await self.gateway.dispatch('gitlab_cli', {'host': 'evil.example',
            'project': 'team/repo', 'args': ['mr', 'list'], 'scope': {'gitlab_mode': 'write'}})
        self.assertFalse(result['ok'])
        self.assertEqual(self.calls, [])

    async def test_prepare_copies_only_safe_source_without_git_or_env(self):
        repo = self.root / '.project' / 'repo'
        repo.mkdir(parents=True)
        (repo / '.git').mkdir()
        (repo / '.git/HEAD').write_text('ref: refs/heads/main\n')
        (repo / '.env').write_text('SECRET')
        (repo / 'main.py').write_text('print(1)')
        (repo / 'leak').symlink_to('/etc/passwd')
        async def git(args, timeout, input_text=None, cwd=None):
            self.calls.append((args, cwd))
            if 'ls-files' in args and '--' not in args:
                return '100644 ' + 'a' * 40 + ' 0\tmain.py\0'
            if 'rev-parse' in args:
                return 'a' * 40
            if 'get-url' in args:
                return 'git@git.example:team/repo.git\n'
            return ''
        self.gateway.run = git
        result = await self.gateway.dispatch('repository_prepare', {'host': 'git.example', 'project': 'team/repo'})
        self.assertTrue(result['ok'], result)
        exported = Path(result['path'])
        self.assertTrue((exported / 'main.py').exists())
        for name in ('.git', '.env', 'leak'):
            self.assertFalse((exported / name).exists())

    async def test_prepare_refuses_reserved_directory_names_before_clone(self):
        for name in ('credential', 'pic', 'reports', 'files', 'skills'):
            with self.subTest(name=name):
                project = 'team/' + name
                self.gateway.scope['projects'] = [{'host': 'git.example', 'project': project}]
                result = await self.gateway.dispatch('repository_prepare', {
                    'host': 'git.example', 'project': project})
                self.assertEqual(result, {'ok': False, 'error': {'type': 'repository_path_collision'}})
                self.assertFalse((self.root / '.project' / name).exists())
        self.assertEqual(self.calls, [])

    async def test_prepare_refuses_existing_reserved_directories_without_inspecting_repositories(self):
        for name in ('credential', 'pic', 'reports', 'files', 'skills'):
            with self.subTest(name=name):
                directory = self.root / '.project' / name
                (directory / '.git').mkdir(parents=True)
                marker = directory / 'service-data'
                marker.write_text('service fixture')
                project = 'team/' + name
                self.gateway.scope['projects'] = [{'host': 'git.example', 'project': project}]
                result = await self.gateway.dispatch('repository_prepare', {
                    'host': 'git.example', 'project': project})
                self.assertEqual(result, {'ok': False, 'error': {'type': 'repository_path_collision'}})
                self.assertEqual(marker.read_text(), 'service fixture')
        self.assertEqual(self.calls, [])

    async def test_prepare_never_scans_fixed_skill_checkout(self):
        skills_repo = self.root / '.project/skills'
        (skills_repo / '.git').mkdir(parents=True)
        marker = skills_repo / 'service-data'
        marker.write_text('service fixture')

        async def run(args, timeout, input_text=None, cwd=None):
            self.calls.append((args, cwd))
            if Path(cwd).is_relative_to(skills_repo):
                self.fail('GitLab preparation inspected the fixed skill checkout')
            if 'config' in args:
                return ''
            raise RuntimeError('stop before synthetic clone')

        self.gateway.run = run
        result = await self.gateway.dispatch('repository_prepare', {'host': 'git.example', 'project': 'team/repo'})
        self.assertFalse(result['ok'])
        self.assertFalse(any(Path(cwd).is_relative_to(skills_repo) for args, cwd in self.calls))
        self.assertTrue(any('clone' in args for args, cwd in self.calls))
        self.assertEqual(marker.read_text(), 'service fixture')

    async def test_prepare_refuses_collision_and_https_origin(self):
        repo = self.root / '.project' / 'repo'
        repo.mkdir(parents=True)
        (repo / '.git').mkdir()
        (repo / '.git/HEAD').write_text('ref: refs/heads/main\n')
        for origin in ('git@git.example:other/repo.git', 'https://git.example/team/repo.git'):
            async def git(*args, **kwargs):
                return origin
            self.gateway.run = git
            result = await self.gateway.dispatch('repository_prepare', {'host': 'git.example', 'project': 'team/repo'})
            self.assertFalse(result['ok'])

    async def test_gitlab_api_is_project_bound_and_rejects_secrets(self):
        for endpoint in ('projects/9/issues', 'projects/team%252Frepo/issues',
                         'projects/team%2Frepo/access_tokens', 'projects/team%2Frepo/jobs/1/artifacts',
                         'projects/team%2Frepo/issues/../variables'):
            result = await self.gateway.dispatch('gitlab_cli', {'host': 'git.example',
                'project': 'team/repo', 'args': ['api', endpoint]})
            self.assertFalse(result['ok'], endpoint)
        result = await self.gateway.dispatch('gitlab_cli', {'host': 'git.example',
            'project': 'team/repo', 'args': ['api', 'projects/team%2Frepo/issues', '--paginate']})
        self.assertTrue(result['ok'])

    async def test_write_publishes_only_sanitized_changes_and_pushes_verified_origin(self):
        original, git = await self.real_repository()
        (original / '.env').write_text('SECRET')
        runner = self.gateway.run
        async def fake_push(args, timeout, input_text=None, cwd=None):
            if 'push' in args:
                self.calls.append((args, cwd))
                return 'pushed'
            return await runner(args, timeout, input_text, cwd)
        self.gateway.run = fake_push
        target = {'host': 'git.example', 'project': 'team/repo'}
        prepared = await self.gateway.dispatch('repository_prepare', target)
        worker = self.gateway.prepared[('git.example', 'team/repo')][0]
        (Path(prepared['path']) / 'main.py').write_text('new')
        for args in (['add', '--all'], ['commit', '-m', 'Update source'], ['push']):
            result = await self.gateway.dispatch('repository_git', {**target, 'args': args})
            self.assertTrue(result['ok'], result)
        self.assertEqual((worker / 'main.py').read_text(), 'new')
        self.assertEqual((original / 'main.py').read_text(), 'old')
        self.assertEqual((original / '.env').read_text(), 'SECRET')
        push = next(args for args, cwd in self.calls if 'push' in args)
        self.assertTrue(push[-1].endswith(':refs/heads/' + prepared['push_branch']))
        self.assertIn('--no-follow-tags', push)
        self.assertIn('--recurse-submodules=no', push)

    async def test_repository_write_rejects_changed_baseline_and_unsafe_new_files(self):
        repo, git = await self.real_repository()
        target = {'host': 'git.example', 'project': 'team/repo'}
        prepared = await self.gateway.dispatch('repository_prepare', target)
        repo = self.gateway.prepared[('git.example', 'team/repo')][0]
        export = Path(prepared['path'])
        (export / '.env').write_text('evil')
        result = await self.gateway.dispatch('repository_git', {**target, 'args': ['add', '--all']})
        self.assertFalse(result['ok'])
        (export / '.env').unlink()
        (export / 'main.py').write_text('new')
        (repo / 'main.py').write_text('other task')
        result = await self.gateway.dispatch('repository_git', {**target, 'args': ['add', '--all']})
        self.assertFalse(result['ok'])
        self.assertEqual((repo / 'main.py').read_text(), 'other task')

    async def test_repository_reads_limit_output_to_exported_safe_paths(self):
        repo = self.root / '.project' / 'repo'
        repo.mkdir(parents=True)
        (repo / '.git').mkdir()
        (repo / '.git/HEAD').write_text('ref: refs/heads/main\n')
        (repo / 'main.py').write_text('source')
        (repo / '.env').write_text('SECRET')
        async def git(args, timeout, input_text=None, cwd=None):
            self.calls.append((args, cwd))
            if 'ls-files' in args and '--' not in args:
                return '100644 ' + 'a' * 40 + ' 0\tmain.py\0'
            if 'rev-parse' in args:
                return 'a' * 40
            if 'get-url' in args:
                return 'git@git.example:team/repo.git'
            return ''
        self.gateway.run = git
        target = {'host': 'git.example', 'project': 'team/repo'}
        await self.gateway.dispatch('repository_prepare', target)
        result = await self.gateway.dispatch('repository_git', {**target, 'args': ['show', 'HEAD']})
        self.assertTrue(result['ok'])
        args = self.calls[-1][0]
        self.assertIn('--', args)
        self.assertEqual(args[args.index('--') + 1:], ['main.py'])

    async def test_write_scope_api_notes_and_service_auth_status_are_bounded(self):
        self.gateway.scope['gitlab_mode'] = 'write'
        target = {'host': 'git.example', 'project': 'team/repo'}
        result = await self.gateway.dispatch('gitlab_cli', {**target, 'args': [
            'api', 'projects/team%2Frepo/issues/2/notes', '--method', 'POST', '--raw-field', 'body=已处理']})
        self.assertTrue(result['ok'])
        result = await self.gateway.dispatch('gitlab_cli', {**target, 'args': ['auth', 'status']})
        self.assertTrue(result['ok'])
        self.assertNotIn('token', json.dumps(result).lower())
        result = await self.gateway.dispatch('gitlab_cli', {**target, 'args': ['mr', 'create']})
        self.assertFalse(result['ok'])

    async def test_send_failure_requires_ok_and_message_id(self):
        path = self.work / 'report.html'
        path.write_text('report')
        result = await self.gateway.dispatch('send_file', {'path': str(path)})
        self.assertFalse(result['ok'])
        self.assertEqual(self.gateway.delivered_files, set())

    async def test_feishu_reads_remain_in_bound_host_adapter(self):
        with patch('assistant.feishu_im_read.execute', return_value={'ok': True, 'data': {}}) as execute:
            result = await self.gateway.dispatch('feishu_read', {'operation': 'search', 'query': '测试'})
        self.assertTrue(result['ok'])
        self.assertEqual(execute.call_args.args[2], 'ou_owner')

    async def test_hardlinked_files_are_never_exported_or_sent(self):
        protected = self.root / 'protected.txt'
        protected.write_text('private')
        os.link(protected, self.work / 'report.txt')
        result = await self.gateway.dispatch('send_file', {'path': str(self.work / 'report.txt')})
        self.assertFalse(result['ok'])
        repo = self.root / '.project/repo'
        repo.mkdir(parents=True)
        (repo / '.git').mkdir()
        (repo / '.git/HEAD').write_text('ref: refs/heads/main\n')
        os.link(protected, repo / 'main.py')
        async def git(args, timeout, input_text=None, cwd=None):
            return ('git@git.example:team/repo.git' if 'get-url' in args else
                    '100644 ' + 'a' * 40 + ' 0\tmain.py\0' if 'ls-files' in args else '')
        self.gateway.run = git
        result = await self.gateway.dispatch('repository_prepare', {'host': 'git.example', 'project': 'team/repo'})
        self.assertFalse(result['ok'])
        self.assertEqual(protected.read_text(), 'private')

    async def test_raced_host_symlink_never_overwrites_protected_inode(self):
        original, git = await self.real_repository()
        protected = self.root / 'protected.txt'
        protected.write_text('private')
        target = {'host': 'git.example', 'project': 'team/repo'}
        prepared = await self.gateway.dispatch('repository_prepare', target)
        worker = self.gateway.prepared[('git.example', 'team/repo')][0]
        (Path(prepared['path']) / 'main.py').write_text('approved')
        replace = os.replace
        def race(source, destination, **kwargs):
            if destination == 'main.py' and Path('/proc/self/fd/' + str(kwargs['dst_dir_fd'])).resolve() == worker:
                os.unlink(destination, dir_fd=kwargs['dst_dir_fd'])
                os.symlink(protected, destination, dir_fd=kwargs['dst_dir_fd'])
            return replace(source, destination, **kwargs)
        with patch.object(agent_tools.os, 'replace', race):
            await self.gateway.dispatch('repository_git', {**target, 'args': ['add', '--all']})
        self.assertEqual(protected.read_text(), 'private')

    async def real_repository(self):
        repo = self.root / '.project/repo'
        repo.mkdir(parents=True)
        self.fixture_active_repo = repo
        def git(*args, input=None):
            return subprocess.run(['git', '-c', 'user.name=Fixture', '-c',
                'user.email=fixture@example.invalid', *args], cwd=self.fixture_active_repo,
                input=input, text=True, capture_output=True, check=True).stdout
        git('init', '--quiet')
        (repo / 'main.py').write_text('old')
        git('add', '--', 'main.py')
        git('commit', '--quiet', '-m', 'Fixture initial source')
        git('remote', 'add', 'origin', 'git@git.example:team/repo.git')
        self.gateway.scope['gitlab_mode'] = 'write'
        async def run(args, timeout, input_text=None, cwd=None):
            self.calls.append((args, cwd))
            if 'fetch' in args:
                # Simulate a scoped remote using already-created local fixture
                # objects. No test may execute an external fetch or clone.
                source, separator, destination = args[-1].partition(':')
                oid = subprocess.run(['git', 'rev-parse', '--verify', source + '^{commit}'],
                    cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()
                subprocess.run(['git', 'update-ref', destination if separator else 'FETCH_HEAD', oid], cwd=cwd,
                    text=True, capture_output=True, check=True)
                return ''
            process = await asyncio.create_subprocess_exec(*args, cwd=cwd,
                env={**os.environ, 'GIT_AUTHOR_NAME': 'Fixture',
                    'GIT_AUTHOR_EMAIL': 'fixture@example.invalid', 'GIT_COMMITTER_NAME': 'Fixture',
                    'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'},
                stdin=asyncio.subprocess.PIPE if input_text else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            output, error = await process.communicate(input_text.encode() if input_text else None)
            if process.returncode:
                raise RuntimeError(error.decode())
            return output.decode()
        self.gateway.run = run
        return repo, git

    async def test_commit_rejects_same_path_index_blob_replacement(self):
        repo, git = await self.real_repository()
        target = {'host': 'git.example', 'project': 'team/repo'}
        prepared = await self.gateway.dispatch('repository_prepare', target)
        repo = self.gateway.prepared[('git.example', 'team/repo')][0]
        self.fixture_active_repo = repo
        (Path(prepared['path']) / 'main.py').write_text('approved')
        result = await self.gateway.dispatch('repository_git', {**target, 'args': ['add', '--all']})
        self.assertTrue(result['ok'], result)
        blob = git('hash-object', '-w', '--stdin', input='unapproved').strip()
        git('update-index', '--cacheinfo', f'100644,{blob},main.py')
        before = git('rev-parse', 'HEAD')
        result = await self.gateway.dispatch('repository_git', {**target, 'args': ['commit', '-m', 'Approved source']})
        self.assertFalse(result['ok'])
        self.assertEqual(before, git('rev-parse', 'HEAD'))

    async def test_real_fixture_normal_commit_has_approved_tree(self):
        repo, git = await self.real_repository()
        target = {'host': 'git.example', 'project': 'team/repo'}
        prepared = await self.gateway.dispatch('repository_prepare', target)
        repo = self.gateway.prepared[('git.example', 'team/repo')][0]
        self.fixture_active_repo = repo
        (Path(prepared['path']) / 'main.py').write_text('approved')
        result = await self.gateway.dispatch('repository_git', {**target, 'args': ['add', '--all']})
        self.assertTrue(result['ok'], result)
        result = await self.gateway.dispatch('repository_git', {**target, 'args': ['commit', '-m', 'Approved source']})
        self.assertTrue(result['ok'], result)
        self.assertEqual(git('show', 'HEAD:main.py'), 'approved')

    async def test_global_gitlab_reads_are_host_bound_bounded_and_scrubbed(self):
        self.gateway.scope['projects'] = []
        self.gateway.cfg['GITLAB_HOST'] = 'git.example'
        async def response(args, timeout, input_text=None, cwd=None):
            self.calls.append((args, cwd))
            return json.dumps({'id': 12, 'name': 'repo', 'default_branch': 'main',
                'runners_token': 'SECRET', 'import_url': 'https://glpat-SECRET@git.example/team/repo', 'ssh_url_to_repo': 'git@git.example:team/repo.git',
                'nested': [{'password': 'SECRET', 'display_name': 'Owner'}]})
        self.gateway.run = response
        for endpoint in ('user', 'projects', 'merge_requests', 'issues', 'projects/12'):
            result = await self.gateway.dispatch('gitlab_cli', {'host': 'git.example',
                'args': ['api', endpoint, '--raw-field', 'per_page=10']})
            self.assertTrue(result['ok'], result)
            self.assertNotIn('SECRET', json.dumps(result))
            self.assertEqual(result['data']['default_branch'], 'main')
        for args in (['api', 'projects', '-X', 'POST'], ['api', 'projects', '-f', 'per_page=9999'],
                     ['api', 'projects/12/variables'], ['api', 'personal_access_tokens']):
            result = await self.gateway.dispatch('gitlab_cli', {'host': 'git.example', 'args': args})
            self.assertFalse(result['ok'])
        result = await self.gateway.dispatch('gitlab_cli', {'host': 'evil.example', 'args': ['api', 'projects']})
        self.assertFalse(result['ok'])

    async def test_send_deduplicates_success_and_refuses_task_internal_files(self):
        path = self.work / 'report.html'
        path.write_text('report')
        async def sent(args, timeout, input_text=None, cwd=None):
            self.calls.append((args, cwd))
            return '{"ok":true,"data":{"message_id":"om_sent"}}'
        self.gateway.run = sent
        for _ in range(2):
            self.assertTrue((await self.gateway.dispatch('send_file', {'path': str(path)}))['ok'])
        self.assertEqual(len(self.calls), 1)
        for name in ('prompt.json', 'result.json', 'response.schema.json', 'tool_client.py'):
            path = self.work / name
            path.write_text('private task internals')
            self.assertFalse((await self.gateway.dispatch('send_file', {'path': str(path)}))['ok'])

    async def test_prepare_uses_only_tracked_source_preserving_git_build_metadata(self):
        repo, git = await self.real_repository()
        (repo / '.gitignore').write_text('deploy-config.json\n')
        (repo / '.gitlab-ci.yml').write_text('build: {}')
        (repo / 'deploy-config.json').write_text('SECRET')
        git('add', '.gitignore', '.gitlab-ci.yml')
        git('commit', '--quiet', '-m', 'Fixture build metadata')
        result = await self.gateway.dispatch('repository_prepare', {'host': 'git.example', 'project': 'team/repo'})
        self.assertTrue(result['ok'], result)
        export = Path(result['path'])
        self.assertTrue((export / '.gitignore').is_file())
        self.assertTrue((export / '.gitlab-ci.yml').is_file())
        self.assertFalse((export / 'deploy-config.json').exists())

    async def test_publish_rechecks_destination_after_export_snapshot(self):
        repo, git = await self.real_repository()
        target = {'host': 'git.example', 'project': 'team/repo'}
        prepared = await self.gateway.dispatch('repository_prepare', target)
        repo = self.gateway.prepared[('git.example', 'team/repo')][0]
        self.fixture_active_repo = repo
        export = Path(prepared['path'])
        (export / 'main.py').write_text('approved')
        original = self.gateway.snapshot
        def race(directory, *args, **kwargs):
            result = original(directory, *args, **kwargs)
            if Path(directory) == export:
                (repo / 'main.py').write_text('concurrent source')
            return result
        with patch.object(self.gateway, 'snapshot', side_effect=race):
            result = await self.gateway.dispatch('repository_git', {**target, 'args': ['add', '--all']})
        self.assertFalse(result['ok'])
        self.assertEqual((repo / 'main.py').read_text(), 'concurrent source')

    async def test_export_skips_submodules_symlinks_and_deleted_tracked_files(self):
        repo, git = await self.real_repository()
        (repo / 'deleted.py').write_text('old')
        (repo / 'source-link').symlink_to('main.py')
        git('add', '--', 'deleted.py', 'source-link')
        git('update-index', '--add', '--cacheinfo', '160000,' + git('rev-parse', 'HEAD').strip() + ',submodule')
        git('commit', '--quiet', '-m', 'Fixture special entries')
        (repo / 'deleted.py').unlink()
        self.gateway.scope['gitlab_mode'] = 'read'
        result = await self.gateway.dispatch('repository_prepare', {'host': 'git.example', 'project': 'team/repo'})
        self.assertTrue(result['ok'], result)
        export = Path(result['path'])
        self.assertTrue((export / 'main.py').is_file())
        for name in ('deleted.py', 'source-link', 'submodule'):
            self.assertFalse((export / name).exists())

    async def test_literal_tracked_wildcard_cannot_expose_filtered_env(self):
        repo, git = await self.real_repository()
        (repo / '*').write_text('literal source')
        (repo / '.env').write_text('SECRET')
        git('--literal-pathspecs', 'add', '--', '*', '.env')
        git('commit', '--quiet', '-m', 'Fixture literal path')
        target = {'host': 'git.example', 'project': 'team/repo'}
        result = await self.gateway.dispatch('repository_prepare', target)
        self.assertTrue(result['ok'], result)
        result = await self.gateway.dispatch('repository_git', {**target, 'args': ['show', 'HEAD']})
        self.assertTrue(result['ok'], result)
        self.assertNotIn('SECRET', result['data'])
        self.assertIn('literal source', result['data'])

    async def test_tree_capture_race_cannot_bless_different_index_content(self):
        repo, git = await self.real_repository()
        target = {'host': 'git.example', 'project': 'team/repo'}
        prepared = await self.gateway.dispatch('repository_prepare', target)
        repo = self.gateway.prepared[('git.example', 'team/repo')][0]
        self.fixture_active_repo = repo
        (Path(prepared['path']) / 'main.py').write_text('approved')
        runner = self.gateway.run
        attacked = False
        async def race(args, timeout, input_text=None, cwd=None):
            nonlocal attacked
            if 'write-tree' in args and not attacked:
                attacked = True
                blob = git('hash-object', '-w', '--stdin', input='unapproved').strip()
                git('update-index', '--cacheinfo', f'100644,{blob},main.py')
            return await runner(args, timeout, input_text, cwd)
        self.gateway.run = race
        result = await self.gateway.dispatch('repository_git', {**target, 'args': ['add', '--all']})
        self.assertFalse(result['ok'])
        self.assertEqual(git('show', 'HEAD:main.py'), 'old')

    async def test_branch_switch_same_sha_before_add_is_rejected(self):
        repo, git = await self.real_repository()
        target = {'host': 'git.example', 'project': 'team/repo'}
        prepared = await self.gateway.dispatch('repository_prepare', target)
        repo = self.gateway.prepared[('git.example', 'team/repo')][0]
        self.fixture_active_repo = repo
        (Path(prepared['path']) / 'main.py').write_text('approved')
        git('checkout', '-qb', 'other')
        result = await self.gateway.dispatch('repository_git', {**target, 'args': ['add', '--all']})
        self.assertFalse(result['ok'])
        self.assertEqual((repo / 'main.py').read_text(), 'old')

    async def test_branch_switch_same_sha_before_commit_is_rejected(self):
        repo, git = await self.real_repository()
        target = {'host': 'git.example', 'project': 'team/repo'}
        prepared = await self.gateway.dispatch('repository_prepare', target)
        repo = self.gateway.prepared[('git.example', 'team/repo')][0]
        self.fixture_active_repo = repo
        (Path(prepared['path']) / 'main.py').write_text('approved')
        self.assertTrue((await self.gateway.dispatch('repository_git', {**target, 'args': ['add', '--all']}))['ok'])
        git('checkout', '-qb', 'other')
        before = git('rev-parse', 'HEAD')
        result = await self.gateway.dispatch('repository_git', {**target, 'args': ['commit', '-m', 'Approved source']})
        self.assertFalse(result['ok'])
        self.assertEqual(git('rev-parse', 'HEAD'), before)

    async def test_branch_switch_same_sha_before_push_is_rejected(self):
        repo, git = await self.real_repository()
        target = {'host': 'git.example', 'project': 'team/repo'}
        prepared = await self.gateway.dispatch('repository_prepare', target)
        repo = self.gateway.prepared[('git.example', 'team/repo')][0]
        self.fixture_active_repo = repo
        (Path(prepared['path']) / 'main.py').write_text('approved')
        self.assertTrue((await self.gateway.dispatch('repository_git', {**target, 'args': ['add', '--all']}))['ok'])
        self.assertTrue((await self.gateway.dispatch('repository_git', {**target, 'args': ['commit', '-m', 'Approved source']}))['ok'])
        git('checkout', '-qb', 'other')
        self.calls.clear()
        result = await self.gateway.dispatch('repository_git', {**target, 'args': ['push']})
        self.assertFalse(result['ok'])
        self.assertFalse(any('push' in args for args, cwd in self.calls))

    async def test_retained_gateway_sends_and_deduplicates_after_socket_cleanup(self):
        async def sent(args, timeout, input_text=None, cwd=None):
            self.calls.append((args, cwd))
            return '{"ok":true,"data":{"message_id":"om_sent"}}'
        factory = agent_tools.tool_gateway(self.cfg, self.root, self.scope, sent)
        async with factory(self.work, Sandbox(self.work)):
            path = self.work / 'report.html'
            path.write_text('report')
        self.assertFalse((self.work / agent_tools.SOCKET_NAME).exists())
        for _ in range(2):
            result = await factory.tools.dispatch('send_file', {'path': str(path)})
            self.assertTrue(result['ok'], result)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(factory.tools.delivered_files, {path})

    async def test_write_prepare_uses_isolated_workbranch_and_preserves_original_clone(self):
        original, git = await self.real_repository()
        original_head = git('rev-parse', 'HEAD')
        original_ref = git('symbolic-ref', 'HEAD')
        target = {'host': 'git.example', 'project': 'team/repo'}
        prepared = await self.gateway.dispatch('repository_prepare', target)
        self.assertTrue(prepared['ok'], prepared)
        self.assertTrue(prepared.get('work_branch', '').startswith('assistant/'))
        worker = self.gateway.prepared[('git.example', 'team/repo')][0]
        self.assertNotEqual(worker, original)
        self.assertEqual(prepared['persistent_repository'], '.project/repo')
        (Path(prepared['path']) / 'main.py').write_text('approved')
        for args in (['add', '--all'], ['commit', '-m', 'Approved isolated repair']):
            result = await self.gateway.dispatch('repository_git', {**target, 'args': args})
            self.assertTrue(result['ok'], result)
        self.assertEqual(git('rev-parse', 'HEAD'), original_head)
        self.assertEqual(git('symbolic-ref', 'HEAD'), original_ref)
        self.assertEqual((original / 'main.py').read_text(), 'old')

    async def test_explicit_source_work_and_push_branch_are_validated(self):
        original, git = await self.real_repository()
        source = git('symbolic-ref', '--short', 'HEAD').strip()
        target = {'host': 'git.example', 'project': 'team/repo'}
        invalid = await self.gateway.dispatch('repository_prepare', {
            **target, 'source_branch': source, 'work_branch': '../escape', 'push_branch': source})
        self.assertFalse(invalid['ok'])
        prepared = await self.gateway.dispatch('repository_prepare', {
            **target, 'source_branch': source, 'work_branch': 'assistant/repair', 'push_branch': source})
        self.assertTrue(prepared['ok'], prepared)
        self.assertEqual(prepared['work_branch'], 'assistant/repair')
        self.assertEqual(prepared['source_branch'], source)
        self.assertEqual(prepared['push_branch'], source)

    async def test_git_text_eol_conversion_stages_and_commits_without_poisoning_task(self):
        original, git = await self.real_repository()
        (original / '.gitattributes').write_text('main.py text eol=crlf\n')
        git('add', '--', '.gitattributes')
        git('commit', '--quiet', '-m', 'Fixture text conversion')
        target = {'host': 'git.example', 'project': 'team/repo'}
        prepared = await self.gateway.dispatch('repository_prepare', target)
        worker = self.gateway.prepared[('git.example', 'team/repo')][0]
        self.fixture_active_repo = worker
        (Path(prepared['path']) / 'main.py').write_bytes(b'approved\r\n')
        for args in (['add', '--all'], ['commit', '-m', 'Approved CRLF source']):
            result = await self.gateway.dispatch('repository_git', {**target, 'args': args})
            self.assertTrue(result['ok'], result)
        self.assertEqual(git('show', 'HEAD:main.py'), 'approved\n')
        self.assertEqual((worker / 'main.py').read_bytes(), b'approved\r\n')

    async def test_job_and_ci_aliases_use_supported_bounded_api_operations(self):
        target = {'host': 'git.example', 'project': 'team/repo'}
        for args, suffix in ((['job', 'trace', '12'], '/jobs/12/trace'),
                             (['job', 'view', '12'], '/jobs/12'),
                             (['ci', 'get', '--pipeline-id', '9'], '/pipelines/9'),
                             (['ci', 'view', '9'], '/pipelines/9')):
            result = await self.gateway.dispatch('gitlab_cli', {**target, 'args': args})
            self.assertTrue(result['ok'], result)
            command = self.calls[-1][0]
            self.assertEqual(command[1], 'api')
            self.assertTrue(command[2].endswith(suffix), command)
        self.gateway.scope['gitlab_mode'] = 'write'
        for args, suffix in ((['job', 'retry', '12'], '/jobs/12/retry'),
                             (['job', 'cancel', '12'], '/jobs/12/cancel'),
                             (['job', 'play', '12'], '/jobs/12/play'),
                             (['ci', 'cancel', 'pipeline', '9'], '/pipelines/9/cancel'),
                             (['ci', 'cancel', 'job', '12'], '/jobs/12/cancel')):
            result = await self.gateway.dispatch('gitlab_cli', {**target, 'args': args})
            self.assertTrue(result['ok'], result)
            command = self.calls[-1][0]
            self.assertEqual(command[1], 'api')
            self.assertTrue(command[2].endswith(suffix), command)
            self.assertIn('POST', command)

    async def test_project_commit_and_branch_metadata_reads_are_supported(self):
        target = {'host': 'git.example', 'project': 'team/repo'}
        for endpoint in ('projects/team%2Frepo/repository/commits/' + 'a' * 40,
                         'projects/team%2Frepo/repository/commits/' + 'a' * 40 + '/diff',
                         'projects/team%2Frepo/repository/branches/feature%2Ffix'):
            result = await self.gateway.dispatch('gitlab_cli', {**target, 'args': ['api', endpoint]})
            self.assertTrue(result['ok'], result)

    async def test_branch_switch_inside_ref_update_never_advances_other_branch(self):
        original, git = await self.real_repository()
        target = {'host': 'git.example', 'project': 'team/repo'}
        prepared = await self.gateway.dispatch('repository_prepare', target)
        self.fixture_active_repo = self.gateway.prepared[('git.example', 'team/repo')][0]
        captured = git('symbolic-ref', 'HEAD').strip()
        old = git('rev-parse', 'HEAD')
        (Path(prepared['path']) / 'main.py').write_text('approved')
        self.assertTrue((await self.gateway.dispatch('repository_git', {**target, 'args': ['add', '--all']}))['ok'])
        runner = self.gateway.run
        async def race(args, timeout, input_text=None, cwd=None):
            if 'update-ref' in args:
                git('checkout', '-qb', 'other')
            return await runner(args, timeout, input_text, cwd)
        self.gateway.run = race
        result = await self.gateway.dispatch('repository_git', {**target, 'args': ['commit', '-m', 'Approved source']})
        self.assertFalse(result['ok'])
        self.assertEqual(git('rev-parse', 'refs/heads/other'), old)
        self.assertNotEqual(git('rev-parse', captured), old)
        self.assertEqual((original / 'main.py').read_text(), 'old')

    async def test_branch_switch_inside_push_cannot_change_explicit_destination(self):
        original, git = await self.real_repository()
        target = {'host': 'git.example', 'project': 'team/repo'}
        prepared = await self.gateway.dispatch('repository_prepare', target)
        self.fixture_active_repo = self.gateway.prepared[('git.example', 'team/repo')][0]
        (Path(prepared['path']) / 'main.py').write_text('approved')
        for args in (['add', '--all'], ['commit', '-m', 'Approved source']):
            self.assertTrue((await self.gateway.dispatch('repository_git', {**target, 'args': args}))['ok'])
        approved = git('rev-parse', 'HEAD').strip()
        runner = self.gateway.run
        pushes = []
        async def race(args, timeout, input_text=None, cwd=None):
            if 'push' in args:
                git('checkout', '-qb', 'other')
                pushes.append(args)
                return 'pushed'
            return await runner(args, timeout, input_text, cwd)
        self.gateway.run = race
        result = await self.gateway.dispatch('repository_git', {**target, 'args': ['push']})
        self.assertTrue(result['ok'], result)
        self.assertEqual(pushes[0][-1], approved + ':refs/heads/' + prepared['push_branch'])

    async def test_encoded_branch_and_project_traversal_are_rejected(self):
        target = {'host': 'git.example', 'project': 'team/repo'}
        result = await self.gateway.dispatch('gitlab_cli', {**target, 'args': [
            'api', 'projects/team%2Frepo/repository/branches/main%2F..%2F..%2Fvariables']})
        self.assertFalse(result['ok'])
        self.gateway.cfg['GITLAB_HOST'] = 'git.example'
        result = await self.gateway.dispatch('gitlab_cli', {'host': 'git.example',
            'args': ['api', 'projects/team%2F..']})
        self.assertFalse(result['ok'])

    async def test_configured_report_origin_port_and_publication_branch_are_preserved(self):
        original, git = await self.real_repository()
        origin = 'ssh://git@git.example:2222/team/repo.git'
        git('remote', 'set-url', 'origin', origin)
        git('branch', 'publication/pages')
        self.gateway.cfg.update({'REPORT_PUBLISH_ENABLED': True,
            'REPORT_PAGES_REPO_URL': origin, 'REPORT_PAGES_BRANCH': 'publication/pages'})
        self.gateway.scope['kind'] = 'report'
        target = {'host': 'git.example', 'project': 'team/repo'}
        prepared = await self.gateway.dispatch('repository_prepare', {**target,
            'source_branch': 'publication/pages', 'push_branch': 'publication/pages'})
        self.assertTrue(prepared['ok'], prepared)
        self.assertEqual(prepared['source_branch'], 'publication/pages')
        self.assertEqual(prepared['push_branch'], 'publication/pages')
        (Path(prepared['path']) / 'main.py').write_text('published report')
        runner = self.gateway.run
        pushes = []
        async def fake_push(args, timeout, input_text=None, cwd=None):
            if 'push' in args:
                pushes.append(args)
                return 'pushed'
            return await runner(args, timeout, input_text, cwd)
        self.gateway.run = fake_push
        for args in (['add', '--all'], ['commit', '-m', 'Approved publication'], ['push']):
            result = await self.gateway.dispatch('repository_git', {**target, 'args': args})
            self.assertTrue(result['ok'], result)
        self.assertTrue(pushes[0][-1].endswith(':refs/heads/publication/pages'))
        self.assertEqual((original / 'main.py').read_text(), 'old')
        self.assertNotIn(':2222', json.dumps(prepared))
        git('remote', 'set-url', 'origin', 'ssh://git@git.example:2223/team/repo.git')
        result = await self.gateway.dispatch('repository_git', {**target, 'args': ['push']})
        self.assertFalse(result['ok'])
        self.assertEqual(len(pushes), 1)

    async def test_report_clone_uses_exact_configured_ssh_url_without_network(self):
        original, git = await self.real_repository()
        origin = 'ssh://git@git.example:2222/team/repo.git'
        git('remote', 'set-url', 'origin', origin)
        git('branch', 'publication/pages')
        template = self.root / 'fixture-template'
        original.rename(template)
        self.fixture_active_repo = template
        self.gateway.cfg.update({'REPORT_PUBLISH_ENABLED': True,
            'REPORT_PAGES_REPO_URL': origin, 'REPORT_PAGES_BRANCH': 'publication/pages'})
        self.gateway.scope['kind'] = 'report'
        runner = self.gateway.run
        clones = []
        async def fake_clone(args, timeout, input_text=None, cwd=None):
            if 'clone' in args:
                clones.append(args)
                shutil.copytree(template, Path(args[-1]))
                return ''
            return await runner(args, timeout, input_text, cwd)
        self.gateway.run = fake_clone
        prepared = await self.gateway.dispatch('repository_prepare', {'host': 'git.example',
            'project': 'team/repo', 'source_branch': 'publication/pages', 'push_branch': 'publication/pages'})
        self.assertTrue(prepared['ok'], prepared)
        self.assertEqual(clones[0][-2], origin)
        self.assertEqual(Path(clones[0][-1]), self.root / '.project/repo')

    async def test_contact_search_is_bound_readonly_and_permission_task_scoped(self):
        result = await self.gateway.dispatch('contact_search', {'query': '小乙'})
        self.assertFalse(result['ok'])
        self.assertEqual(self.calls, [])
        self.gateway.scope['gitlab_mode'] = 'write'
        async def lark(args, timeout, input_text=None, cwd=None):
            self.calls.append((args, cwd))
            if args[1:3] == ['auth', 'status']:
                return json.dumps({'identities': {'user': {'openId': 'ou_owner',
                    'scope': ['contact:user:search']}}})
            return json.dumps({'ok': True, 'data': {'has_more': False, 'users': [{
                'open_id': 'ou_target', 'localized_name': '小乙', 'enterprise_email': 'example@example.invalid',
                'department': 'Engineering', 'token': 'SECRET', 'is_cross_tenant': False}]}})
        self.gateway.run = lark
        result = await self.gateway.dispatch('contact_search', {'query': '小乙'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['data']['users'][0]['enterprise_email'], 'example@example.invalid')
        self.assertNotIn('SECRET', json.dumps(result))
        args = self.calls[-1][0]
        self.assertEqual(args[1:3], ['contact', '+search-user'])
        self.assertIn('--exclude-external-users', args)
        self.assertIn('--page-size', args)
        self.assertFalse((await self.gateway.dispatch('contact_search', {'query': '小乙', 'file': '/etc/passwd'}))['ok'])
        self.gateway.cfg['FEISHU_OWNER_OPEN_ID'] = 'ou_other'
        before = len([args for args, cwd in self.calls if '+search-user' in args])
        self.assertFalse((await self.gateway.dispatch('contact_search', {'query': '小乙'}))['ok'])
        self.assertEqual(len([args for args, cwd in self.calls if '+search-user' in args]), before)

    async def test_gitlab_user_lookup_and_scoped_membership_management(self):
        self.gateway.scope['gitlab_mode'] = 'write'
        target = {'host': 'git.example', 'project': 'team/repo'}
        for endpoint in ('users?search=example%40example.invalid', 'users/12'):
            result = await self.gateway.dispatch('gitlab_cli', {**target, 'args': ['api', endpoint]})
            self.assertTrue(result['ok'], result)
        for endpoint, method, fields in (
            ('projects/team%2Frepo/members/all/12', 'GET', []),
            ('projects/team%2Frepo/members', 'POST', ['user_id=12', 'access_level=30']),
            ('projects/team%2Frepo/members/12', 'PUT', ['access_level=40', 'expires_at=2026-12-31']),
            ('projects/team%2Frepo/members/12', 'DELETE', [])):
            args = ['api', endpoint, '--method', method]
            for value in fields:
                args += ['--raw-field', value]
            result = await self.gateway.dispatch('gitlab_cli', {**target, 'args': args})
            self.assertTrue(result['ok'], result)
        for args in (['api', 'users/12', '-X', 'DELETE'],
                     ['api', 'projects/team%2Frepo/members', '-X', 'POST', '-f', 'body=not-allowed'],
                     ['api', 'projects/team%2Frepo/members/12', '-X', 'PUT', '-f', 'access_level=999'],
                     ['api', 'projects/team%2Frepo/members/12', '-X', 'PUT', '-f', 'expires_at=not-a-date']):
            self.assertFalse((await self.gateway.dispatch('gitlab_cli', {**target, 'args': args}))['ok'])
        self.gateway.scope['gitlab_mode'] = 'read'
        self.assertFalse((await self.gateway.dispatch('gitlab_cli', {**target,
            'args': ['api', 'projects/team%2Frepo/members/all']}))['ok'])

    async def test_read_report_default_exports_the_resolved_publication_branch(self):
        original, git = await self.real_repository()
        primary = git('symbolic-ref', '--short', 'HEAD').strip()
        origin = 'ssh://git@git.example:2222/team/repo.git'
        git('remote', 'set-url', 'origin', origin)
        git('checkout', '-qb', 'publication/pages')
        (original / 'main.py').write_text('PAGES')
        git('add', '--', 'main.py')
        git('commit', '--quiet', '-m', 'Fixture published content')
        publication = git('rev-parse', 'HEAD').strip()
        git('checkout', primary)
        self.gateway.cfg.update({'REPORT_PUBLISH_ENABLED': True,
            'REPORT_PAGES_REPO_URL': origin, 'REPORT_PAGES_BRANCH': 'publication/pages'})
        self.gateway.scope.update({'kind': 'report', 'gitlab_mode': 'read'})
        prepared = await self.gateway.dispatch('repository_prepare', {'host': 'git.example', 'project': 'team/repo'})
        self.assertTrue(prepared['ok'], prepared)
        self.assertEqual(prepared['source_branch'], 'publication/pages')
        self.assertEqual(prepared['source_commit'], publication)
        self.assertEqual((Path(prepared['path']) / 'main.py').read_text(), 'PAGES')
        self.assertEqual((original / 'main.py').read_text(), 'old')
        self.assertEqual(git('symbolic-ref', '--short', 'HEAD').strip(), primary)

    async def test_scoped_pages_metadata_is_readonly(self):
        target = {'host': 'git.example', 'project': 'team/repo'}
        result = await self.gateway.dispatch('gitlab_cli', {**target,
            'args': ['api', 'projects/team%2Frepo/pages']})
        self.assertTrue(result['ok'], result)
        self.gateway.scope['gitlab_mode'] = 'write'
        result = await self.gateway.dispatch('gitlab_cli', {**target,
            'args': ['api', 'projects/team%2Frepo/pages', '-X', 'DELETE']})
        self.assertFalse(result['ok'])

    async def test_explicit_source_branch_refreshes_cached_remote_content(self):
        original, git = await self.real_repository()
        primary = git('symbolic-ref', '--short', 'HEAD').strip()
        git('branch', 'publication/pages')
        git('checkout', '-qb', 'fixture-remote')
        (original / 'main.py').write_text('FRESH_REMOTE')
        git('add', '--', 'main.py')
        git('commit', '--quiet', '-m', 'Fixture remote advance')
        remote_commit = git('rev-parse', 'HEAD').strip()
        git('checkout', primary)
        runner = self.gateway.run
        fetches = []
        async def fake_remote(args, timeout, input_text=None, cwd=None):
            if 'fetch' in args:
                fetches.append(args)
                git('update-ref', args[-1].partition(':')[2] or 'FETCH_HEAD', remote_commit)
                return ''
            return await runner(args, timeout, input_text, cwd)
        self.gateway.run = fake_remote
        prepared = await self.gateway.dispatch('repository_prepare', {'host': 'git.example',
            'project': 'team/repo', 'source_branch': 'publication/pages', 'push_branch': 'publication/pages'})
        self.assertTrue(prepared['ok'], prepared)
        self.assertEqual(prepared['source_commit'], remote_commit)
        self.assertEqual((Path(prepared['path']) / 'main.py').read_text(), 'FRESH_REMOTE')
        self.assertTrue(fetches[0][-1].startswith('refs/heads/publication/pages:refs/agent-tasks/'))
        self.assertEqual((original / 'main.py').read_text(), 'old')

    async def test_parallel_branch_fetches_resolve_task_specific_source_refs(self):
        original, git = await self.real_repository()
        primary = git('symbolic-ref', '--short', 'HEAD').strip()
        commits = {}
        for branch in ('branchA', 'branchB'):
            git('checkout', '-qb', branch)
            (original / 'main.py').write_text(branch)
            git('add', '--', 'main.py')
            git('commit', '--quiet', '-m', 'Fixture ' + branch)
            commits[branch] = git('rev-parse', 'HEAD').strip()
            git('checkout', primary)
        work_b = self.root / 'work-b'
        work_b.mkdir()
        second = agent_tools.TaskTools(self.cfg, self.root, self.gateway.scope, self.gateway.run,
                                       work_b, Sandbox(work_b))
        runner = self.gateway.run
        fetched_a, fetched_b = asyncio.Event(), asyncio.Event()
        fetch_commands = []
        async def interleave(args, timeout, input_text=None, cwd=None):
            if 'fetch' in args:
                fetch_commands.append(args)
                source, separator, destination = args[-1].partition(':')
                branch = source.removeprefix('refs/heads/')
                git('update-ref', destination if separator else 'FETCH_HEAD', commits[branch])
                if branch == 'branchA':
                    fetched_a.set()
                    await asyncio.wait_for(fetched_b.wait(), 5)
                else:
                    fetched_b.set()
                return ''
            return await runner(args, timeout, input_text, cwd)
        self.gateway.run = interleave
        second.run = interleave
        target = {'host': 'git.example', 'project': 'team/repo'}
        first_task = asyncio.create_task(self.gateway.dispatch('repository_prepare', {
            **target, 'source_branch': 'branchA'}))
        await asyncio.wait_for(fetched_a.wait(), 5)
        second_result = await second.dispatch('repository_prepare', {**target, 'source_branch': 'branchB'})
        first_result = await first_task
        for branch, result in (('branchA', first_result), ('branchB', second_result)):
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['source_commit'], commits[branch])
            self.assertEqual((Path(result['path']) / 'main.py').read_text(), branch)
        for command in fetch_commands:
            self.assertIn('--no-write-fetch-head', command)
            self.assertIn(':refs/agent-tasks/', command[-1])
        self.assertNotEqual(fetch_commands[0][-1].split(':')[1], fetch_commands[1][-1].split(':')[1])

if __name__ == '__main__':
    unittest.main()
