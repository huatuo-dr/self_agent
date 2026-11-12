import asyncio
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from assistant import evolution, evolution_worker, service
from assistant.sessions import Sessions


class EvolutionValidationIsolationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('bwrap'), 'Linux bwrap required')
    def test_validation_masks_custom_profile_inside_a_runtime_mount(self):
        with tempfile.TemporaryDirectory(prefix='agent-profile-mount-',dir='/var/tmp') as directory:
            base=Path(directory);root=base/'deployment';root.mkdir()
            runtime=base/'runtime';runtime.mkdir()
            profile=runtime/'identity.json';profile.write_text('private fixture')
            (runtime/'public.txt').write_text('runtime fixture')
            source=base/'verify';source.mkdir()
            code=('import pathlib,json\np=pathlib.Path\ntry:\n'
                  f' profile_hidden=p({str(profile)!r}).read_text()==""\n'
                  'except PermissionError:\n profile_hidden=True\n'
                  'print(json.dumps({"profile_hidden":profile_hidden,'
                  f'"runtime":p({str(runtime / "public.txt")!r}).read_text()'+'}))')
            from assistant.agent_isolation import SYSTEM_RUNTIME_ROOTS
            with patch.object(evolution_worker,'ROOT',root), \
                    patch('assistant.agent_isolation.SYSTEM_RUNTIME_ROOTS',(*SYSTEM_RUNTIME_ROOTS,runtime)):
                args=evolution_worker._sandbox_command(source,['/usr/bin/python3','-c',code],
                                                       cfg={'OWNER_PROFILE_PATH':str(profile)})
            result=subprocess.run(args,capture_output=True,text=True,timeout=10,
                                  env=evolution_worker._sandbox_environment())
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout),{'profile_hidden':True,'runtime':'runtime fixture'})

    def test_validation_copy_excludes_custom_profile_and_external_hardlink(self):
        with tempfile.TemporaryDirectory(prefix='agent-validation-copy-') as directory:
            base=Path(directory);root=base/'source';root.mkdir()
            (root/'feature.py').write_text('value=1\n')
            (root/'identity.json').write_text('{"name":"private fixture"}')
            private=base/'private';private.write_text('external fixture')
            os.link(private,root/'ordinary.txt')
            (root/'.env.production').write_text('hidden fixture')
            folder=base/'task';folder.mkdir()
            inventory={name:None for name in ('feature.py','identity.json','ordinary.txt','.env.production')}
            with patch.object(evolution_worker,'ROOT',root):
                work=evolution_worker._verified_source(folder,inventory,cfg={'OWNER_PROFILE_PATH':'identity.json'})
            self.assertEqual({p.name for p in work.iterdir()},{'feature.py'})

    @unittest.skipUnless(shutil.which('bwrap') and Path('/var/tmp').is_dir(), 'Linux bwrap required')
    def test_generated_tests_cannot_read_host_deployment_or_other_host_files(self):
        with tempfile.TemporaryDirectory(prefix='agent-evolution-test-',dir='/var/tmp') as directory:
            base=Path(directory);root=base/'deployment';root.mkdir()
            (root/'.env').write_text('fixture-secret')
            unrelated=base/'private.txt';unrelated.write_text('other-fixture-secret')
            source=base/'verify';source.mkdir();(source/'sample.py').write_text('value=1\n')
            code=('import json,pathlib; p=pathlib.Path; '
                  f'print(json.dumps({{"deployment_visible":p({str(root / ".env")!r}).exists(),'
                  f'"other_visible":p({str(unrelated)!r}).exists(),'
                  '"source_readable":p("sample.py").is_file()}));'
                  'p("result.txt").write_text("test output")')
            with patch.object(evolution_worker,'ROOT',root):
                args=evolution_worker._sandbox_command(source,['/usr/bin/python3','-c',code])
            result=subprocess.run(args,capture_output=True,text=True,timeout=10,
                                  env=evolution_worker._sandbox_environment(),check=True)
            self.assertEqual(json.loads(result.stdout),{'deployment_visible':False,'other_visible':False,
                                                       'source_readable':True})
            self.assertEqual((source/'result.txt').read_text(),'test output')


class EvolutionConversationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = service.database(':memory:')
        self.sessions = Sessions(self.db, 'oc_owner')
        self.cfg = {'FEISHU_OWNER_CHAT_ID': 'oc_owner', 'FEISHU_OWNER_OPEN_ID': 'ou_owner',
                    'AGENT_HISTORY_TURNS': 10, 'LARK_CLI_BIN': 'fake', 'EVOLUTION_ENABLED': True}

    def tearDown(self):
        self.db.close()

    def incoming(self, mid, content, thread=None, root=None):
        self.db.execute('''INSERT INTO inbox(id,binding,content,kind,acknowledged,
                          thread_id,root_id,thread_checked) VALUES(?,? ,?,'text',1,?,?,1)''',
                        (mid, 'oc_owner', content, thread, root))
        self.db.commit()

    async def process(self, mid, proposal=None):
        async def send(args, *unused, **kwargs):
            if '--markdown' not in args:
                raise AssertionError(args)
            return json.dumps({'ok': True, 'data': {
                'message_id': 'om_reply_' + mid, 'thread_id': 'omt_evolution'}})
        discussion = AsyncMock(return_value=proposal)
        with patch.object(service, 'run_command', send), patch.object(evolution, 'discuss', discussion), \
                patch.object(service, 'answer', AsyncMock(side_effect=AssertionError('ordinary answer called'))):
            worker = asyncio.create_task(service.worker(self.cfg, self.db))
            try:
                for _ in range(300):
                    row = self.db.execute('SELECT state FROM inbox WHERE id=?', (mid,)).fetchone()
                    if row and row[0] == 'done':
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(self.db.execute('SELECT state FROM inbox WHERE id=?',
                                                 (mid,)).fetchone()[0], 'done')
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        return discussion

    async def test_command_creates_topic_and_waits_for_requirement(self):
        self.incoming('om_start', '/evolution')
        discussion = await self.process('om_start')
        discussion.assert_not_awaited()
        task = evolution.get(self.db, 'oc_owner', 1)
        self.assertEqual(task['state'], 'discussing')
        self.assertEqual(self.sessions.thread(1)['root_id'], 'om_start')
        self.assertIn('本话题说明目标', self.db.execute(
            "SELECT reply FROM inbox WHERE id='om_start'").fetchone()[0])

    async def test_goal_discussion_and_explicit_delivered_approval(self):
        self.incoming('om_start', '/evolution 支持一种新图片格式')
        proposal = {'reply': '已核实目前的图片入口。',
                    'plan': '扩展图片格式检查与下载处理，并添加格式样本测试；测试通过后自动重启服务，不提交代码。',
                    'ready': True}
        await self.process('om_start', proposal)
        task = evolution.get(self.db, 'oc_owner', 1)
        self.assertEqual(task['state'], 'planned')
        self.assertEqual(task['plan_source'], 'om_start')
        self.assertIn('同意实施', self.db.execute(
            "SELECT reply FROM inbox WHERE id='om_start'").fetchone()[0])
        self.incoming('om_approve', '同意实施', 'omt_evolution', 'om_start')
        await self.process('om_approve')
        self.assertEqual(evolution.get(self.db, 'oc_owner', 1)['state'], 'approved')
        self.assertEqual(evolution.get(self.db, 'oc_owner', 1)['approval_source'], 'om_approve')

    def test_approval_requires_delivered_latest_plan_in_same_session(self):
        sid = self.sessions.create('om_start', '开发目标')
        evolution.create(self.db, 'oc_owner', sid)
        self.db.execute("INSERT INTO inbox(id,binding,content,kind,state,session_id) "
                        "VALUES('om_plan','oc_owner','方案','text','pending',?)", (sid,))
        evolution.record_discussion(self.db, 'oc_owner', sid, 'om_plan', {
            'reply': '方案', 'plan': '完成这个功能并且运行完整测试，测试通过之后重启服务，不进行 Git 提交。', 'ready': True})
        self.assertIn('尚未成功送达', evolution.approve(self.db, 'oc_owner', sid, 'om_approve'))
        self.db.execute("UPDATE inbox SET state='done' WHERE id='om_plan'")
        self.db.commit()
        self.assertIn('已收到实施授权', evolution.approve(self.db, 'oc_owner', sid, 'om_approve'))
        self.assertEqual(evolution.get(self.db, 'oc_owner', sid)['state'], 'approved')
        self.assertIsNone(evolution.get(self.db, 'oc_other', sid))

    async def test_scheduler_starts_only_after_approval_reply_is_delivered(self):
        sid = self.sessions.create('om_start', '开发目标')
        self.sessions.bind(sid, 'om_start', 'omt_evolution')
        evolution.create(self.db, 'oc_owner', sid)
        self.db.execute("INSERT INTO inbox(id,binding,content,kind,state,session_id) "
                        "VALUES('om_plan','oc_owner','方案','text','done',?)", (sid,))
        evolution.record_discussion(self.db, 'oc_owner', sid, 'om_plan', {
            'reply': '方案', 'plan': '完成这个功能并且运行完整测试，测试通过之后重启服务，不进行 Git 提交。', 'ready': True})
        self.assertIn('已收到实施授权', evolution.approve(self.db, 'oc_owner', sid, 'om_approve'))
        self.db.execute("INSERT INTO inbox(id,binding,content,kind,state,session_id) "
                        "VALUES('om_approve','oc_owner','同意实施','text','pending',?)", (sid,))
        self.db.commit()
        starts = []
        class FakeProcess:
            pid = os.getpid()
        def start(*args, **kwargs):
            starts.append(args)
            return FakeProcess()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(evolution, 'STATE', Path(directory)), \
                patch.object(evolution.subprocess, 'Popen', start):
            scheduler = asyncio.create_task(evolution.scheduler(self.cfg, self.db, AsyncMock()))
            try:
                await asyncio.sleep(0.05)
                self.assertFalse(starts)
                self.db.execute("UPDATE inbox SET state='done' WHERE id='om_approve'")
                self.db.commit()
                await asyncio.sleep(2.1)
                self.assertEqual(len(starts), 1)
                self.assertEqual(evolution.get(self.db, 'oc_owner', sid)['state'], 'running')
            finally:
                scheduler.cancel()
                await asyncio.gather(scheduler, return_exceptions=True)

    async def test_finished_job_notifies_original_topic_once(self):
        sid = self.sessions.create('om_start', '开发目标')
        self.sessions.bind(sid, 'om_start', 'omt_evolution')
        evolution.create(self.db, 'oc_owner', sid)
        self.db.execute("UPDATE evolution_tasks SET state='completed',"
                        "approval_source='om_approve',final_message='已重启并验证' WHERE session_id=?", (sid,))
        self.db.commit()
        calls = []
        async def send(args, timeout):
            calls.append(args)
            return json.dumps({'ok': True, 'data': {'message_id': 'om_final'}})
        task = evolution.get(self.db, 'oc_owner', sid)
        await evolution._send_notice(self.cfg, self.db, task, send)
        self.assertEqual(evolution.get(self.db, 'oc_owner', sid)['notified'], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][calls[0].index('--message-id') + 1], 'om_start')
        self.assertIn('--reply-in-thread', calls[0])
        self.assertIn('--idempotency-key', calls[0])
        changed = {**task, 'final_message': '修复后已重启并验证'}
        self.assertNotEqual(evolution.notice_key(task), evolution.notice_key(changed))

    async def test_failed_run_can_be_revised_and_approved_in_same_topic(self):
        self.incoming('om_start', '/evolution 支持图片')
        first = {'reply': '方案一', 'plan': '实现图片处理和相关测试，验证成功后重启服务并保留报告，不提交代码。', 'ready': True}
        await self.process('om_start', first)
        self.incoming('om_approve1', '同意实施', 'omt_evolution', 'om_start')
        await self.process('om_approve1')
        self.db.execute("UPDATE evolution_tasks SET state='failed',final_message='上一轮开发失败',"
                        "notified=0 WHERE session_id=1")
        self.db.commit()
        self.incoming('om_followup', '请修正失败原因，继续支持图片', 'omt_evolution', 'om_start')
        second = {'reply': '已调整方案', 'plan': '修正失败原因并实现图片处理，补充测试后重启服务并保留报告，不提交代码。', 'ready': True}
        discussion = await self.process('om_followup', second)
        self.assertEqual(discussion.await_args.kwargs['last_result'], '上一轮开发失败')
        task = evolution.get(self.db, 'oc_owner', 1)
        self.assertEqual(task['state'], 'planned')
        self.assertEqual(task['approval_source'], 'om_approve1')
        self.assertEqual(task['final_message'], '上一轮开发失败')
        self.incoming('om_approve2', '同意实施', 'omt_evolution', 'om_start')
        await self.process('om_approve2')
        self.assertIn('上一轮开发结果仍在发送', self.db.execute(
            "SELECT reply FROM inbox WHERE id='om_approve2'").fetchone()[0])
        async def send(*unused):
            return json.dumps({'ok': True, 'data': {'message_id': 'om_notice'}})
        await evolution._send_notice(self.cfg, self.db, evolution.get(self.db, 'oc_owner', 1), send)
        self.assertEqual(evolution.get(self.db, 'oc_owner', 1)['notified'], 1)
        self.incoming('om_approve3', '同意实施', 'omt_evolution', 'om_start')
        await self.process('om_approve3')
        task = evolution.get(self.db, 'oc_owner', 1)
        self.assertEqual(task['state'], 'approved')
        self.assertEqual(task['approval_source'], 'om_approve3')
        self.assertEqual(task['final_message'], '')
        self.db.execute("UPDATE evolution_tasks SET state='completed',final_message='图片功能已上线',"
                        "notified=1 WHERE session_id=1")
        self.db.commit()
        self.incoming('om_more', '图片再增加一种格式', 'omt_evolution', 'om_start')
        discussion = await self.process('om_more', second)
        self.assertEqual(discussion.await_args.kwargs['last_result'], '图片功能已上线')
        self.assertEqual(evolution.get(self.db, 'oc_owner', 1)['state'], 'planned')
        self.assertEqual(self.sessions.thread(1)['root_id'], 'om_start')

    async def test_bare_command_after_completion_invites_same_topic_followup(self):
        self.incoming('om_start', '/evolution')
        await self.process('om_start')
        self.db.execute("UPDATE evolution_tasks SET state='completed' WHERE session_id=1")
        self.db.commit()
        self.incoming('om_status', '/evolution', 'omt_evolution', 'om_start')
        discussion = await self.process('om_status')
        discussion.assert_not_awaited()
        self.assertIn('本话题直接说明调整', self.db.execute(
            "SELECT reply FROM inbox WHERE id='om_status'").fetchone()[0])

    def test_repairing_state_remains_active_and_recovers_interruption(self):
        sid = self.sessions.create('om_start', '开发目标')
        evolution.create(self.db, 'oc_owner', sid)
        self.db.execute("UPDATE evolution_tasks SET state='repairing' WHERE session_id=?", (sid,))
        self.db.commit()
        self.assertTrue(evolution.active(self.db))
        self.assertIn('正在根据验证结果修复', evolution.status_text(
            evolution.get(self.db, 'oc_owner', sid)))
        evolution._mark_interrupted(self.db, sid)
        self.assertEqual(evolution.get(self.db, 'oc_owner', sid)['state'], 'interrupted')


class EvolutionSnapshotTests(unittest.TestCase):
    def test_no_source_change_reports_agent_blocker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            (root / 'feature.py').write_text('original\n')
            subprocess.run(['git', 'add', 'feature.py'], cwd=root, check=True)
            subprocess.run(['git', '-c', 'user.email=test@example.com',
                            '-c', 'user.name=Tester', 'commit', '-qm', 'baseline'], cwd=root, check=True)
            db_path = root / '.local' / 'messages.sqlite3'
            db_path.parent.mkdir()
            db = service.database(db_path)
            sid = Sessions(db, 'oc_owner').create('om_start', '开发目标')
            evolution.create(db, 'oc_owner', sid)
            db.execute("INSERT INTO inbox(id,binding,content,kind,state,session_id) "
                       "VALUES('om_approve','oc_owner','同意实施','text','done',?)", (sid,))
            db.execute("UPDATE evolution_tasks SET state='running',plan='测试方案',"
                       "approval_source='om_approve' WHERE session_id=?", (sid,))
            db.commit()
            db.close()
            original_database = service.database
            def blocked(cfg, prompt, timeout, final_path, approval_ref):
                final_path.write_text(
                    'BLOCKED: 无法在禁网开发进程中验证所需飞书接口和权限。')
                return 0
            with patch.object(evolution_worker, 'ROOT', root), \
                    patch.object(evolution_worker, 'STATE', root / '.local'), \
                    patch.object(service, 'config', return_value={
                        'FEISHU_OWNER_CHAT_ID': 'oc_owner', 'CODEX_BIN': 'fake',
                        'AGENT_TIMEOUT_SECONDS': 10, 'LARK_CLI_BIN': 'fake'}), \
                    patch.object(service, 'database', side_effect=lambda _: original_database(db_path)), \
                    patch.object(service, 'current', return_value={'pid': os.getpid()}), \
                    patch.object(evolution_worker, '_run_agent', side_effect=blocked):
                self.assertEqual(evolution_worker.run(sid), 1)
            folder = evolution.run_folder(sid, 'om_approve', root / '.local')
            self.assertIn('无法在禁网开发进程中验证', (folder / 'report.md').read_text())
            self.assertFalse((folder / 'agent-final.tmp').exists())
            self.assertEqual((root / 'feature.py').read_text(), 'original\n')

    def test_next_iteration_inherits_previous_uncommitted_code_and_rollback_preserves_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=root, check=True)
            subprocess.run(['git', 'config', 'user.name', 'Tester'], cwd=root, check=True)
            (root / 'feature.py').write_text('original\n')
            subprocess.run(['git', 'add', 'feature.py'], cwd=root, check=True)
            subprocess.run(['git', 'commit', '-qm', 'baseline'], cwd=root, check=True)
            (root / 'feature.py').write_text('first evolution\n')
            folder = root / '.local' / 'evolution' / '2'
            with patch.object(evolution_worker, 'ROOT', root):
                snapshot = evolution_worker._snapshot(folder)
                self.assertEqual((folder / 'before' / 'feature.py').read_text(), 'first evolution\n')
                (root / 'feature.py').write_text('second evolution\n')
                (root / 'new_feature.py').write_text('new file\n')
                after = evolution_worker._inventory()
                self.assertEqual(evolution_worker._changes(snapshot['files'], after),
                                 ['feature.py', 'new_feature.py'])
                evolution_worker._preserve_attempt(folder, snapshot['files'], after)
                evolution_worker._restore(folder, snapshot['files'], after)
                self.assertEqual((root / 'feature.py').read_text(), 'first evolution\n')
                self.assertFalse((root / 'new_feature.py').exists())
                self.assertEqual((folder / 'attempted' / 'feature.py').read_text(),
                                 'second evolution\n')

    def _run_job(self, fail_restart, no_change_first=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=root, check=True)
            subprocess.run(['git', 'config', 'user.name', 'Tester'], cwd=root, check=True)
            (root / 'feature.py').write_text('previous evolution\n')
            subprocess.run(['git', 'add', 'feature.py'], cwd=root, check=True)
            subprocess.run(['git', 'commit', '-qm', 'baseline'], cwd=root, check=True)
            # An earlier approved evolution remains uncommitted in main.
            (root / 'feature.py').write_text('previous uncommitted evolution\n')
            db_path = root / '.local' / 'messages.sqlite3'
            db_path.parent.mkdir()
            db = service.database(db_path)
            sessions = Sessions(db, 'oc_owner')
            sid = sessions.create('om_start', '开发目标')
            sessions.bind(sid, 'om_start', 'omt_evolution')
            evolution.create(db, 'oc_owner', sid)
            db.execute("INSERT INTO inbox(id,binding,content,kind,state,session_id) "
                       "VALUES('om_approve','oc_owner','同意实施','text','done',?)", (sid,))
            db.execute("UPDATE evolution_tasks SET state='running',plan='实现测试功能并重启',"
                       "approval_source='om_approve',worker_pid=? WHERE session_id=?", (os.getpid(), sid))
            db.commit()
            db.close()
            ready = root / '.local' / 'ready'
            ready.write_text('codex')
            original_database = service.database
            labels, prompts = [], []
            def develop(cfg, prompt, timeout, final_path, approval_ref):
                prompts.append(prompt)
                if no_change_first and len(prompts) == 1:
                    return 0
                (root / 'feature.py').write_text('new evolution\n')
                return 0
            def command(args, folder, label, timeout, input_text=None, env=None):
                labels.append(label)
                return 1 if fail_restart and label == 'restart' else 0
            with patch.object(evolution_worker, 'ROOT', root), \
                    patch.object(evolution_worker, 'STATE', root / '.local'), \
                    patch.object(service, 'config', return_value={
                        'FEISHU_OWNER_CHAT_ID': 'oc_owner', 'CODEX_BIN': 'fake',
                        'AGENT_TIMEOUT_SECONDS': 10, 'LARK_CLI_BIN': 'fake'}), \
                    patch.object(service, 'database', side_effect=lambda _: original_database(db_path)), \
                    patch.object(service, 'current', return_value={'pid': os.getpid()}), \
                    patch.object(service, 'READY', ready), \
                    patch.object(evolution_worker, '_run_agent', side_effect=develop), \
                    patch.object(evolution_worker, '_sandbox_command',
                                 side_effect=lambda source, command, **kwargs: command), \
                    patch.object(evolution_worker, '_run_command', side_effect=command):
                exit_code = evolution_worker.run(sid)
            db = original_database(db_path)
            task = evolution.get(db, 'oc_owner', sid)
            db.close()
            folder = evolution.run_folder(sid, 'om_approve', root / '.local')
            self.assertEqual(labels[:3],
                             ['compile-2', 'tests-2', 'restart'] if no_change_first else
                             ['compile', 'tests', 'restart'])
            if no_change_first:
                self.assertEqual(len(prompts), 2)
                self.assertIn('Development produced no source changes', prompts[1])
            self.assertTrue((folder / 'report.md').is_file())
            if fail_restart:
                self.assertEqual(exit_code, 1)
                self.assertEqual(task['state'], 'failed')
                self.assertEqual(labels[-1], 'rollback-restart')
                self.assertEqual((root / 'feature.py').read_text(), 'previous uncommitted evolution\n')
                self.assertEqual((folder / 'attempted' / 'feature.py').read_text(), 'new evolution\n')
            else:
                self.assertEqual(exit_code, 0)
                self.assertEqual(task['state'], 'completed')
                self.assertEqual((root / 'feature.py').read_text(), 'new evolution\n')
                self.assertIn('监听就绪', task['final_message'])

    def test_worker_validates_and_restarts_without_commit(self):
        self._run_job(False)

    def test_worker_retries_no_source_change_without_new_approval(self):
        self._run_job(False, no_change_first=True)

    def test_worker_restores_previous_uncommitted_code_when_restart_fails(self):
        self._run_job(True)

    def _run_repair_job(self, always_fail):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=root, check=True)
            subprocess.run(['git', 'config', 'user.name', 'Tester'], cwd=root, check=True)
            (root / 'feature.py').write_text('baseline\n')
            subprocess.run(['git', 'add', 'feature.py'], cwd=root, check=True)
            subprocess.run(['git', 'commit', '-qm', 'baseline'], cwd=root, check=True)
            db_path = root / '.local' / 'messages.sqlite3'
            db_path.parent.mkdir()
            db = service.database(db_path)
            sid = Sessions(db, 'oc_owner').create('om_start', '开发目标')
            evolution.create(db, 'oc_owner', sid)
            db.execute("INSERT INTO inbox(id,binding,content,kind,state,session_id) "
                       "VALUES('om_approve','oc_owner','同意实施','text','done',?)", (sid,))
            db.execute("UPDATE evolution_tasks SET state='running',plan='批准的修复范围',"
                       "approval_source='om_approve' WHERE session_id=?", (sid,))
            db.commit()
            db.close()
            ready = root / '.local' / 'ready'
            ready.write_text('codex')
            prompts, labels = [], []
            def develop(cfg, prompt, timeout, final_path, approval_ref):
                prompts.append(prompt)
                (root / 'feature.py').write_text(f'round {len(prompts)}\n')
                return 0
            def command(args, folder, label, timeout, input_text=None, env=None):
                labels.append(label)
                if label.startswith('tests') and (always_fail or label == 'tests'):
                    (folder / f'{label}.log').write_text(
                        'ERROR: test_regression (tests.test_feature.Case)\nFAILED (errors=1)\n')
                    return 1
                return 0
            original_database = service.database
            with patch.object(evolution_worker, 'ROOT', root), \
                    patch.object(evolution_worker, 'STATE', root / '.local'), \
                    patch.object(service, 'config', return_value={
                        'FEISHU_OWNER_CHAT_ID': 'oc_owner', 'CODEX_BIN': 'fake',
                        'AGENT_TIMEOUT_SECONDS': 10, 'LARK_CLI_BIN': 'fake'}), \
                    patch.object(service, 'database', side_effect=lambda _: original_database(db_path)), \
                    patch.object(service, 'current', return_value={'pid': os.getpid()}), \
                    patch.object(service, 'READY', ready), \
                    patch.object(evolution_worker, '_run_agent', side_effect=develop), \
                    patch.object(evolution_worker, '_sandbox_command', side_effect=lambda source, cmd, **kwargs: cmd), \
                    patch.object(evolution_worker, '_run_command', side_effect=command):
                exit_code = evolution_worker.run(sid)
            folder = evolution.run_folder(sid, 'om_approve', root / '.local')
            db = original_database(db_path)
            task = evolution.get(db, 'oc_owner', sid)
            state = task['state']
            db.close()
            self.assertIn('test_regression', prompts[1])
            self.assertTrue(all('批准的修复范围' in prompt for prompt in prompts))
            self.assertEqual(len(prompts), 3 if always_fail else 2)
            self.assertEqual(labels[:2], ['compile', 'tests'])
            self.assertEqual(labels[2:4], ['compile-2', 'tests-2'])
            self.assertEqual(len(json.loads((folder / 'attempts.json').read_text())), len(prompts))
            if always_fail:
                self.assertEqual(exit_code, 1)
                self.assertEqual(state, 'failed')
                self.assertNotIn('restart', labels)
                self.assertEqual((root / 'feature.py').read_text(), 'baseline\n')
                self.assertEqual((folder / 'attempted' / 'feature.py').read_text(), 'round 3\n')
                self.assertIn('automatic repair limit reached', (folder / 'report.md').read_text())
            else:
                self.assertEqual(exit_code, 0)
                self.assertEqual(state, 'completed')
                self.assertEqual(labels[-1], 'restart')
                self.assertEqual((root / 'feature.py').read_text(), 'round 2\n')
                self.assertIn('共 2 轮', (folder / 'report.md').read_text())
                self.assertIn('tests.log', (folder / 'report.md').read_text())
                self.assertIn('test_regression', task['final_message'])

    def test_worker_repairs_failed_test_within_approved_run(self):
        self._run_repair_job(False)

    def test_worker_rolls_back_only_after_repair_limit(self):
        self._run_repair_job(True)


if __name__ == '__main__':
    unittest.main()
