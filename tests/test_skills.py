import asyncio
from datetime import datetime
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from assistant import skills


URL = 'git@example.test:team/catalog.git'


class SkillsTests(unittest.TestCase):
    def test_command_metadata_search_and_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            path = base / 'SKILL.md'
            path.write_text('---\nname: issue\ndescription: >\n  创建 Issue\n  查询 Issue\n---\nBody')
            item = skills.metadata(path, base)
            self.assertEqual(item['description'], '创建 Issue 查询 Issue')
            self.assertEqual(item['path'], 'SKILL.md')
        index = [dict(name=f'issue-{n}', description='创建 Issue', path=f'skills/{n}/SKILL.md') for n in range(7)]
        self.assertEqual(len(skills.search(index, '提交issue')), 5)
        self.assertEqual(len(skills.search(index, '提交issue', limit=3)), 3)
        self.assertEqual(skills.search(index, '完全无关'), [])
        self.assertIn('中文', skills.tokens('中文 search'))

    def test_configuration_is_opt_in_ssh_only_and_branch_validated(self):
        skills.validate_config({'SKILLS_REPO_ENABLED': False, 'SKILLS_REPO_URL': 'bad'})
        for url in ('', '/tmp/local', 'file:///tmp/local', 'https://example.test/team/x.git',
                    'git@example.test:../x.git', 'ssh://root@example.test/team/x.git'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                skills.validate_config({'SKILLS_REPO_ENABLED': True, 'SKILLS_REPO_URL': url})
        for branch in ('-bad', 'a..b', 'HEAD', 'a.lock', 'a b'):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                skills.validate_config({'SKILLS_REPO_ENABLED': True, 'SKILLS_REPO_URL': URL,
                                        'SKILLS_REPO_BRANCH': branch})
        skills.validate_config({'SKILLS_REPO_ENABLED': True, 'SKILLS_REPO_URL': URL})

    def test_usage_does_not_sync_and_failure_does_not_search(self):
        async def check():
            repo = skills.SkillRepository({'SKILLS_REPO_URL': URL}, Path('/tmp/test'), Path('/tmp/test/state'), AsyncMock())
            repo.sync = AsyncMock(side_effect=RuntimeError('secret'))
            self.assertIn('用法', await repo.handle_command(''))
            repo.sync.assert_not_called()
            with patch.object(skills, 'search', side_effect=AssertionError('must not search')):
                self.assertIn('更新失败', await repo.handle_command('issue'))
        asyncio.run(check())

    def test_due_uses_nine_and_restart_catchup(self):
        repo = skills.SkillRepository({'SKILLS_REPO_URL': URL, 'TIMEZONE': 'America/New_York'}, Path('/tmp/test'), Path('/tmp/test/state'), AsyncMock())
        tz = ZoneInfo('America/New_York')
        self.assertFalse(repo.due(datetime(2026, 7, 1, 8, 59, tzinfo=tz), {'date': '2026-06-30'}))
        self.assertTrue(repo.due(datetime(2026, 7, 1, 9, 0, tzinfo=tz), {'date': '2026-06-30'}))
        self.assertFalse(repo.due(datetime(2026, 7, 1, 9, 0, tzinfo=tz), {'date': '2026-07-01'}))
        self.assertTrue(repo.due(datetime(2026, 7, 1, 8, 0, tzinfo=tz), None))

    def test_scheduler_retries_then_polls(self):
        async def check():
            repo = skills.SkillRepository({'SKILLS_REPO_URL': URL}, Path('/tmp/test'), Path('/tmp/test/state'), AsyncMock())
            repo.cached = lambda: None
            repo.sync = AsyncMock(side_effect=[RuntimeError('failure'), {}])
            pauses = []
            async def pause(seconds):
                pauses.append(seconds)
                if len(pauses) == 2:
                    raise asyncio.CancelledError()
            with patch.object(skills.asyncio, 'sleep', pause):
                with self.assertRaises(asyncio.CancelledError):
                    await repo.scheduler()
            self.assertEqual(pauses, [60, 30])
        asyncio.run(check())

    def test_factory_rebuilds_after_configuration_change(self):
        cfg = {'SKILLS_REPO_URL': URL}
        first = skills.repository(cfg, Path('/tmp/test'), Path('/tmp/test/state'), AsyncMock())
        self.assertIs(first, skills.repository(cfg, Path('/tmp/test'), Path('/tmp/test/state'), AsyncMock()))
        cfg['SKILLS_REPO_BRANCH'] = 'topic'
        self.assertIsNot(first, skills.repository(cfg, Path('/tmp/test'), Path('/tmp/test/state'), AsyncMock()))


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.remote = self.root / 'remote'
        self.remote.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.test')
        self.write('skills/issue/SKILL.md', '---\nname: issue\ndescription: 创建 Issue\n---\nversion one')
        self.write('skills/issue/references/help.md', 'reference')
        self.write('skills/issue/scripts/run.py', 'print("hello")')
        self.write('skills/other/SKILL.md', '---\nname: other\ndescription: unrelated\n---')
        self.commit()
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        return subprocess.check_output(['git', '-C', str(cwd or self.remote), *args], stderr=subprocess.DEVNULL).decode().strip()

    def write(self, path, text):
        target = self.remote / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-m', 'fixture')

    async def transport(self, args, timeout, cwd=None, **kwargs):
        self.calls.append(args)
        # Production only accepts SSH. The fixture redirects transport locally,
        # while retaining SSH origin metadata and all actual Git operations.
        actual = [*args[:1], '-c', 'protocol.file.allow=always', *args[1:]]
        if 'clone' in actual:
            actual[actual.index(URL)] = str(self.remote)
        elif 'ls-remote' in actual:
            actual[actual.index(URL)] = str(self.remote)
        elif 'fetch' in actual:
            actual[actual.index('origin')] = str(self.remote)
            actual += ['+refs/heads/*:refs/remotes/origin/*']
        elif 'set-head' in actual:
            return self.git('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main', cwd=Path(cwd))
        result = subprocess.run(actual, cwd=cwd, env=kwargs.get('env'), capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('fixture git failure')
        if 'clone' in actual:
            self.git('remote', 'set-url', 'origin', URL, cwd=Path(actual[-1]))
        return result.stdout

    def repo(self, **cfg):
        return skills.SkillRepository({'SKILLS_REPO_URL': URL, **cfg}, self.root, self.root / '.local', self.transport)

    def test_clone_fixed_path_update_immutable_snapshot_and_staging(self):
        async def check():
            repo = self.repo()
            first = await repo.sync()
            self.assertEqual(repo.repo, self.root / '.project/skills')
            matches = skills.search(first['index'], 'issue', limit=3)
            with skills.task_materials(first, matches) as staged:
                self.assertFalse(staged.is_relative_to(self.root))
                self.assertTrue((staged / 'skills/issue/SKILL.md').is_file())
                self.assertTrue((staged / 'skills/issue/scripts/run.py').is_file())
                self.assertFalse((staged / 'skills/other').exists())
                staged_path = staged
            self.assertFalse(staged_path.exists())
            self.write('skills/issue/SKILL.md', '---\nname: issue\ndescription: 创建 Issue\n---\nversion two')
            self.commit()
            self.assertIn('**issue**', await repo.handle_command('issue'))
            second = repo.cached()
            self.assertNotEqual(first['commit'], second['commit'])
            with skills.task_materials(first, matches) as staged:
                self.assertIn('version one', (staged / 'skills/issue/SKILL.md').read_text())
            self.assertTrue(any('core.hooksPath=/dev/null' in call for call in self.calls))
        asyncio.run(check())

    def test_root_level_skill_can_stage_its_directory(self):
        async def check():
            self.write('skills/SKILL.md', '---\nname: catalog-root\ndescription: root-level catalog\n---\nRoot skill')
            self.write('skills/references/root.md', 'Root reference')
            self.commit()
            data = await self.repo().sync()
            matches = skills.search(data['index'], 'catalog-root')
            self.assertEqual([item['path'] for item in matches], ['skills/SKILL.md'])
            with skills.task_materials(data, matches) as staged:
                self.assertIn('Root skill', (staged / 'skills/SKILL.md').read_text())
                self.assertEqual((staged / 'skills/references/root.md').read_text(), 'Root reference')
                self.assertFalse((staged / 'skills/issue').exists())
                self.assertFalse((staged / 'skills/other').exists())
        asyncio.run(check())

    def test_parent_skill_includes_nested_skill_only_when_explicitly_referenced(self):
        async def check():
            self.write('skills/issue/nested/SKILL.md', '---\nname: nested\ndescription: nested guidance\n---\nNested')
            self.write('skills/issue/nested/scripts/helper.py', 'print("nested")')
            self.commit()
            repo = self.repo()
            data = await repo.sync()
            matches = [item for item in data['index'] if item['name'] == 'issue']
            with skills.task_materials(data, matches) as staged:
                self.assertFalse((staged / 'skills/issue/nested').exists())
                self.assertTrue((staged / 'skills/issue/scripts/run.py').is_file())
            self.write('skills/issue/SKILL.md', '---\nname: issue\ndescription: Issue\n---\n'
                       '[nested](nested/SKILL.md)')
            self.commit()
            data = await repo.sync()
            with skills.task_materials(data, matches) as staged:
                self.assertTrue((staged / 'skills/issue/nested/SKILL.md').is_file())
                self.assertTrue((staged / 'skills/issue/nested/scripts/helper.py').is_file())
                self.assertFalse((staged / 'skills/other').exists())
        asyncio.run(check())

    def test_invalid_first_clone_never_publishes_checkout(self):
        async def check():
            for unsafe in ('.env', 'skills/issue/escape'):
                with self.subTest(unsafe=unsafe):
                    path = self.remote / unsafe
                    if unsafe == '.env':
                        self.write(unsafe, 'SYNTHETIC_FIXTURE=value')
                    else:
                        path.symlink_to('/synthetic-outside')
                    self.commit()
                    repo = self.repo()
                    with self.assertRaises(ValueError):
                        await repo.sync()
                    self.assertFalse(repo.repo.exists())
                    self.assertIsNone(repo.cached())
                    path.unlink()
                    self.commit()
        asyncio.run(check())

    def test_invalid_update_keeps_previous_checkout_and_cache(self):
        async def check():
            repo = self.repo()
            first = await repo.sync()
            self.write('.env', 'SYNTHETIC_FIXTURE=value')
            self.commit()
            with self.assertRaises(ValueError):
                await repo.sync()
            self.assertEqual(self.git('rev-parse', 'HEAD', cwd=repo.repo), first['commit'])
            self.assertFalse((repo.repo / '.env').exists())
            self.assertEqual(repo.cached()['commit'], first['commit'])
            (self.remote / '.env').unlink()
            self.commit()
            self.assertNotEqual((await repo.sync())['commit'], first['commit'])
        asyncio.run(check())

    def test_export_ignore_cannot_hide_unsafe_checkout_files(self):
        async def check():
            self.write('.env', 'SYNTHETIC_FIXTURE=value')
            self.write('.gitattributes', '.env export-ignore\n')
            self.commit()
            repo = self.repo()
            with self.assertRaises(ValueError):
                await repo.sync()
            self.assertFalse(repo.repo.exists())
        asyncio.run(check())

    def test_missing_configured_branch_is_reported_without_fallback(self):
        async def check():
            repo = self.repo(SKILLS_REPO_BRANCH='missing-fixture-branch')
            result = await repo.handle_command('issue')
            self.assertIn('分支', result)
            self.assertIn('missing-fixture-branch', result)
            self.assertIn('不存在', result)
            self.assertFalse(repo.repo.exists())
            repo = self.repo(SKILLS_REPO_BRANCH='main')
            first = await repo.sync()
            self.git('switch', '-c', 'replacement')
            self.git('branch', '-D', 'main')
            result = await repo.handle_command('issue')
            self.assertIn('main', result)
            self.assertIn('不存在', result)
            self.assertEqual(repo.cached()['commit'], first['commit'])
        asyncio.run(check())

    def test_executable_skill_script_remains_executable_and_read_only(self):
        async def check():
            name = 'skills/issue/scripts/run.py'
            self.write(name, '#!/usr/bin/env python3\nprint("hello")\n')
            (self.remote / name).chmod(0o755)
            self.commit()
            data = await self.repo().sync()
            with skills.task_materials(data, skills.search(data['index'], 'issue')) as staged:
                result = subprocess.run([str(staged / name)], capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout, 'hello\n')
                self.assertEqual((staged / name).stat().st_mode & 0o7777, 0o500)
                self.assertEqual((staged / 'skills/issue/SKILL.md').stat().st_mode & 0o7777, 0o400)
            self.assertEqual((Path(data['snapshot']) / name).stat().st_mode & 0o7777, 0o500)
        asyncio.run(check())

    def test_explicit_relative_document_references_stage_bounded_closure(self):
        async def check():
            self.write('skills/task/SKILL.md', '---\nname: task-guidance\ndescription: task guidance\n---\n'
                       '[shared](../shared/SKILL.md) [web](https://example.test/doc.md) [anchor](#part)')
            self.write('skills/shared/SKILL.md', '---\nname: shared\ndescription: shared guidance\n---\n'
                       '[common](../../references/common.md)')
            self.write('skills/shared/scripts/helper.py', 'print("shared")')
            self.write('references/common.md', '[cycle](../skills/shared/SKILL.md)\nCommon reference')
            self.write('references/unrelated.md', 'Must remain excluded')
            self.commit()
            data = await self.repo().sync()
            matches = [item for item in data['index'] if item['name'] == 'task-guidance']
            with skills.task_materials(data, matches) as staged:
                self.assertTrue((staged / 'skills/task/SKILL.md').is_file())
                self.assertTrue((staged / 'skills/shared/SKILL.md').is_file())
                self.assertTrue((staged / 'skills/shared/scripts/helper.py').is_file())
                self.assertIn('Common reference', (staged / 'references/common.md').read_text())
                self.assertFalse((staged / 'references/unrelated.md').exists())
                self.assertFalse((staged / 'skills/issue').exists())
                self.assertFalse((staged / 'skills/other').exists())
        asyncio.run(check())

    def test_relative_document_reference_cannot_escape_snapshot(self):
        async def check():
            self.write('skills/issue/SKILL.md', '---\nname: issue\ndescription: Issue\n---\n'
                       '[outside](../../../outside.md)')
            self.commit()
            data = await self.repo().sync()
            with self.assertRaises(ValueError):
                with skills.task_materials(data, skills.search(data['index'], 'issue')):
                    pass
        asyncio.run(check())

    def test_relative_reference_checks_referenced_file_hardlinks(self):
        async def check():
            import os
            self.write('skills/issue/SKILL.md', '---\nname: issue\ndescription: Issue\n---\n'
                       '[common](../../references/common.md)')
            self.write('references/common.md', 'Common reference')
            self.commit()
            data = await self.repo().sync()
            os.link(Path(data['snapshot']) / 'references/common.md', self.root / 'external-reference')
            with self.assertRaises(ValueError):
                with skills.task_materials(data, skills.search(data['index'], 'issue')):
                    pass
        asyncio.run(check())

    def test_dirty_origin_branch_and_cache_binding_rejections(self):
        async def check():
            repo = self.repo()
            await repo.sync()
            other = skills.SkillRepository({'SKILLS_REPO_URL': 'git@elsewhere.test:team/catalog.git'}, self.root, self.root / '.local', self.transport)
            self.assertIsNone(other.cached())
            branch = self.repo(SKILLS_REPO_BRANCH='main')
            self.assertIsNone(branch.cached())
            (repo.repo / 'untracked').write_text('keep')
            with self.assertRaises(ValueError):
                await repo.sync()
            (repo.repo / 'untracked').unlink()
            self.git('switch', '-c', 'local', cwd=repo.repo)
            with self.assertRaises(ValueError):
                await repo.sync()
            self.git('switch', 'main', cwd=repo.repo)
            self.git('remote', 'set-url', 'origin', 'git@elsewhere.test:team/catalog.git', cwd=repo.repo)
            with self.assertRaises(ValueError):
                await repo.sync()
        asyncio.run(check())

    def test_explicit_branch_first_clone(self):
        async def check():
            self.git('switch', '-c', 'topic')
            self.write('skills/issue/references/help.md', 'topic reference')
            self.commit()
            self.git('switch', 'main')
            repo = self.repo(SKILLS_REPO_BRANCH='topic')
            data = await repo.sync()
            self.assertEqual(self.git('branch', '--show-current', cwd=repo.repo), 'topic')
            self.assertEqual(data['branch'], 'topic')
            self.assertIn('--branch', next(c for c in self.calls if 'clone' in c))
        asyncio.run(check())

    def test_normalized_origin_and_early_sync_timezone(self):
        async def check():
            repo = self.repo(TIMEZONE='America/New_York')
            now = datetime(2026, 7, 1, 8, 30, tzinfo=ZoneInfo('America/New_York'))
            with patch.object(skills, 'datetime') as clock:
                clock.now.return_value = now
                data = await repo.sync()
                clock.now.assert_called_once_with(ZoneInfo('America/New_York'))
            self.assertEqual(data['date'], '2026-06-30')
            self.git('remote', 'set-url', 'origin', 'ssh://git@EXAMPLE.TEST:22/team/catalog.git', cwd=repo.repo)
            await repo.sync()
            repo.sync = AsyncMock(side_effect=AssertionError('cache should be used'))
            self.assertEqual((await repo.current())['commit'], data['commit'])
        asyncio.run(check())

    def test_git_errors_are_sanitized(self):
        async def check():
            repo = self.repo()
            repo.run = AsyncMock(side_effect=RuntimeError('PRIVATE SSH ERROR'))
            with self.assertRaisesRegex(RuntimeError, '^Skill repository Git operation failed$'):
                await repo.git('fetch', 'origin')
            args, kwargs = repo.run.call_args
            self.assertEqual(kwargs['env']['GIT_TERMINAL_PROMPT'], '0')
            self.assertEqual(kwargs['env']['GIT_CONFIG_NOSYSTEM'], '1')
            self.assertFalse('GIT_TEMPLATE_DIR' in kwargs['env'])
        asyncio.run(check())

    def test_archive_rejects_protected_and_oversized_files(self):
        async def check():
            self.write('.env', 'secret')
            self.commit()
            repo = self.repo()
            with self.assertRaises(ValueError):
                await repo.sync()
            (self.remote / '.env').unlink()
            self.write('skills/issue/assets/large', 'x' * 100)
            self.commit()
            with patch.object(skills, 'MAX_FILE_BYTES', 50), self.assertRaises(ValueError):
                await repo.sync()
        asyncio.run(check())

    def test_local_filters_are_rejected_before_status(self):
        async def check():
            repo = self.repo()
            await repo.sync()
            self.git('config', 'filter.test.clean', 'must-never-run', cwd=repo.repo)
            self.calls.clear()
            with self.assertRaises(ValueError):
                await repo.sync()
            self.assertFalse(any('status' in call for call in self.calls))
        asyncio.run(check())

    def test_worktree_redirect_is_rejected_before_mutation(self):
        async def check():
            import shutil
            repo = self.repo()
            await repo.sync()
            external = self.root / 'outside-fixed-clone'
            shutil.copytree(repo.repo / 'skills', external / 'skills')
            self.git('config', 'core.worktree', str(external), cwd=repo.repo)
            self.write('skills/issue/references/help.md', 'updated outside fixed clone')
            self.commit()
            self.calls.clear()
            with self.assertRaises(ValueError):
                await repo.sync()
            self.assertEqual((external / 'skills/issue/references/help.md').read_text(), 'reference')
            self.assertFalse(any('fetch' in call or 'merge' in call for call in self.calls))
        asyncio.run(check())

    def test_common_git_directory_redirect_is_rejected(self):
        async def check():
            import shutil
            repo = self.repo()
            await repo.sync()
            external = self.root / 'outside-git-metadata'
            shutil.copytree(repo.repo / '.git', external)
            (repo.repo / '.git/commondir').write_text(str(external))
            self.calls.clear()
            with self.assertRaises(ValueError):
                await repo.sync()
            self.assertFalse(any('fetch' in call or 'merge' in call for call in self.calls))
        asyncio.run(check())

    def test_nested_git_metadata_symlink_is_rejected_before_fetch(self):
        async def check():
            import shutil
            repo = self.repo()
            await repo.sync()
            refs = repo.repo / '.git/refs/remotes/origin'
            external = self.root / 'external-remote-refs'
            shutil.copytree(refs, external)
            shutil.rmtree(refs)
            refs.symlink_to(external, target_is_directory=True)
            self.assertFalse((external / 'main').exists())
            self.write('skills/issue/references/help.md', 'new version')
            self.commit()
            self.calls.clear()
            with self.assertRaises(ValueError):
                await repo.sync()
            self.assertFalse((external / 'main').exists())
            self.assertFalse(any('fetch' in call for call in self.calls))
        asyncio.run(check())

    def test_hardlinked_mutable_git_metadata_is_rejected(self):
        async def check():
            import os
            repo = self.repo()
            await repo.sync()
            config = repo.repo / '.git/config'
            os.link(config, self.root / 'external-git-config')
            self.calls.clear()
            with self.assertRaises(ValueError):
                await repo.sync()
            self.assertFalse(any('fetch' in call for call in self.calls))
        asyncio.run(check())

    def test_staging_rejects_hardlinks(self):
        async def check():
            repo = self.repo()
            data = await repo.sync()
            target = Path(data['snapshot']) / 'skills/issue/references/help.md'
            import os
            os.link(target, self.root / 'hardlink')
            with self.assertRaises(ValueError):
                with skills.task_materials(data, skills.search(data['index'], 'issue')):
                    pass
        asyncio.run(check())

    def test_symlink_and_protected_archive_rejected_without_cache(self):
        async def check():
            (self.remote / 'skills/issue/escape').symlink_to('/etc/passwd')
            self.commit()
            repo = self.repo()
            with self.assertRaises(ValueError):
                await repo.sync()
            self.assertIsNone(repo.cached())
        asyncio.run(check())

    def test_staging_rejects_modified_snapshot_and_forged_selection(self):
        async def check():
            repo = self.repo()
            data = await repo.sync()
            with self.assertRaises(ValueError):
                with skills.task_materials(data, [{'path': '../../etc/passwd'}]):
                    pass
            target = Path(data['snapshot']) / 'skills/issue/references/help.md'
            target.chmod(0o600)
            target.write_text('modified')
            with self.assertRaises(ValueError):
                with skills.task_materials(data, skills.search(data['index'], 'issue')):
                    pass
        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
