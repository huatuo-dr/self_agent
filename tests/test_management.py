import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from assistant import service
from assistant.sessions import Sessions
from assistant.management import Management, POLICIES


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.db = service.database(':memory:')
        self.s = Sessions(self.db, 'oc_owner')
        self.m = Management(self.db, 'oc_owner')

    def tearDown(self):
        self.db.close()

    def make(self, turns=0, days=0, saved=False, source=None):
        source = source or 'source'+str(self.db.execute('SELECT count(*) FROM sessions').fetchone()[0])
        sid = self.s.create(source, '测试主题')
        for i in range(turns):
            self.s.finish(sid, f'{source}_{i}', '问题', '回答', {'status':'已完成'})
        self.db.execute('UPDATE sessions SET last_used=?,saved=? WHERE id=?', (time.time()-days*86400, int(saved),sid))
        self.db.commit()
        return sid

    def preview(self, policy, source='om_clean'):
        self.db.execute("INSERT INTO inbox(id,binding,content,kind,state) VALUES(?,'oc_owner',?,'text','pending')",(source,f'/clean {policy}'))
        self.db.commit()
        text = self.m.clean(str(policy),source)
        row = self.db.execute('SELECT token,candidates FROM clean_plans WHERE source=?',(source,)).fetchone()
        self.db.execute("UPDATE inbox SET state='done' WHERE id=?",(source,))
        self.db.commit()
        return text, row[0] if row else None, json.loads(row[1]) if row else []

    def test_policy_numbers_turn_boundary_and_saved_protection(self):
        a=self.make(4,8)
        b=self.make(5,31)
        c=self.make(0,40,True)
        for policy, expected in [(1,{a}),(2,{b}),(3,{a,b}),(4,{a,b})]:
            text, token, snapshot=self.preview(policy,'preview'+str(policy))
            self.assertEqual({x['id'] for x in snapshot},expected)
            self.assertNotIn(c, {x['id'] for x in snapshot})
        self.assertIn('30 天',POLICIES[2])
        self.assertIn('7 天',POLICIES[3])
        self.assertIn('跳过',self.m.clean('', 'usage'))
        self.assertIn('参数无效',self.m.clean('5','bad'))

    def test_save_unsave_and_views_do_not_refresh_last_use(self):
        sid=self.make(1,40)
        before=self.s.get(sid)['last_used']
        self.assertIn('已保存',self.m.save('',True,sid))
        self.assertIn('⭐ 已保存',self.s.handle('session','', 'view')[0])
        self.assertIn('⭐ 已保存',self.s.handle('session',str(sid), 'view')[0])
        self.assertEqual(self.s.get(sid)['last_used'],before)
        self.assertIn('没有可清理',self.preview(4)[0])
        self.assertIn('取消',self.m.save(str(sid),False))
        self.assertEqual(self.s.get(sid)['last_used'],before)
        self.assertIn(sid,{x['id'] for x in self.preview(2,'second')[2]})
        self.assertIn('未找到',Management(self.db,'other').save(str(sid),True))

    def test_confirmation_rechecks_only_snapshot_and_is_idempotent(self):
        a,b,c=(self.make() for _ in range(3))
        text,token,_=self.preview(4)
        self.m.save(str(a),True)
        self.s.finish(b,'changed','新增问题','回答')
        self.db.commit()
        later=self.make()
        reply=self.m.confirm(token)
        self.assertIn('已删除 1 个',reply)
        self.assertIsNone(self.s.get(c))
        for sid in (a,b,later): self.assertIsNotNone(self.s.get(sid))
        self.assertEqual(self.m.confirm(token),reply)
        self.assertIn('没有找到',Management(self.db,'other').confirm(token))

    def test_preview_must_be_delivered_and_unexpired(self):
        sid=self.make()
        _,token,_=self.preview(4)
        self.db.execute("UPDATE inbox SET state='pending' WHERE id='om_clean'")
        self.db.commit()
        self.assertIn('尚未成功送达',self.m.confirm(token))
        self.db.execute("UPDATE inbox SET state='done' WHERE id='om_clean'")
        self.db.execute('UPDATE clean_plans SET created=0 WHERE token=?',(token,))
        self.db.commit()
        self.assertIn('已过期',self.m.confirm(token))
        self.assertIsNotNone(self.s.get(sid))

    def test_busy_pending_quotes_and_notifications_are_protected(self):
        a,b,c,d=(self.make() for _ in range(4))
        self.db.execute("UPDATE sessions SET status='处理中' WHERE id=?",(a,))
        self.db.execute("INSERT INTO inbox(id,binding,session_id,state) VALUES('pending','oc_owner',?,'pending')",(b,))
        self.s.bind(c,'om_bot','omt_c')
        self.db.execute("INSERT INTO inbox(id,binding,root_id,thread_id,state) VALUES('quoted','oc_owner','om_bot','omt_c','pending')")
        from assistant.issues import IssueAssistant
        IssueAssistant({'GITLAB_HOST':'gitlab.example.com','FEISHU_OWNER_CHAT_ID':'oc_owner'},self.db,AsyncMock(),Path('.'),Path('.'))
        self.db.execute("INSERT INTO issue_notices(id,scope,source,payload,state,session_id) VALUES('notice','scope','orig','{}','draft',?)",(d,))
        self.db.commit()
        self.assertIn('没有可清理',self.preview(4)[0])
        self.assertEqual(self.db.execute('SELECT count(*) FROM sessions').fetchone()[0],4)

    def test_delete_removes_content_preserves_dedup_and_current_is_cleared(self):
        sid=self.make(2)
        self.db.execute("INSERT INTO inbox(id,binding,content,reply,quote,kind,state,session_id) VALUES('om_user','oc_owner','secret','reply','quote','text','done',?)",(sid,))
        self.s.map_message('om_bot',sid,'om_user','secret reply','assistant')
        self.db.commit()
        _,token,_=self.preview(4)
        self.m.confirm(token)
        self.assertEqual(self.s.history(sid,10),[])
        self.assertIsNone(self.s.reference('om_bot'))
        self.assertTrue(self.s.deleted_reference('om_bot'))
        self.assertTrue(self.s.deleted_reference('om_user'))
        self.assertEqual(self.db.execute("SELECT content,reply,quote,state FROM inbox WHERE id='om_user'").fetchone(),('','',None,'done'))
        cfg={'FEISHU_OWNER_CHAT_ID':'oc_owner','FEISHU_OWNER_OPEN_ID':'ou_owner'}
        self.assertFalse(service.enqueue(self.db,cfg,{'chat_type':'p2p','sender_type':'user','sender_id':'ou_owner','chat_id':'oc_owner','message_id':'om_user','message_type':'text','content':'secret'}))
        with patch.object(service,'run_command',AsyncMock(side_effect=AssertionError('must not refetch'))):
            result=asyncio.run(service.quoted_message(cfg,self.db,'om_bot'))
        self.assertIn('已清理',result['error'])

    def test_confirmation_rechecks_queued_work_and_rolls_back_on_failure(self):
        a,b=(self.make() for _ in range(2))
        _,token,_=self.preview(4)
        self.db.execute("INSERT INTO inbox(id,binding,session_id,state) VALUES('late','oc_owner',?,'pending')",(a,))
        self.db.commit()
        delete=self.m.delete
        def fail(sid):
            delete(sid)
            raise RuntimeError('database transaction must roll back')
        with patch.object(self.m,'delete',fail):
            with self.assertRaises(RuntimeError):
                self.m.confirm(token)
        self.assertIsNotNone(self.s.get(a))
        self.assertIsNotNone(self.s.get(b))
        self.assertIsNone(self.db.execute('SELECT result FROM clean_plans WHERE token=?',(token,)).fetchone()[0])
        self.assertIn('已删除 1 个',self.m.confirm(token))
        self.assertIsNotNone(self.s.get(a))
        self.assertIsNone(self.s.get(b))

    def test_age_boundary_is_strict(self):
        sid=self.make()
        for policy,days in [(2,30),(3,7)]:
            self.db.execute('UPDATE sessions SET last_used=? WHERE id=?',(10000000-days*86400,sid))
            self.assertFalse(self.m.matches(self.s.get(sid),policy,10000000))
            self.assertTrue(self.m.matches(self.s.get(sid),policy,10000001))

    def test_status_shows_recent_error_without_secrets(self):
        from assistant.management import record_error
        with tempfile.TemporaryDirectory() as tmp:
            record_error(self.db, '查询失败')
            cfg={'AGENT_BACKEND':'codex','API_TOKEN':'not-for-display',
                 '_runtime':{'started':time.monotonic(),'listener':'状态未知'}}
            text=self.m.status(cfg,Path(tmp))
            self.assertIn('查询失败',text)
            self.assertIn('CLI 默认',text)
            self.assertIn('状态未知',text)
            self.assertNotIn('not-for-display',text)

    def test_saved_protection_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'db'
            db=service.database(path)
            s=Sessions(db,'owner')
            sid=s.create('source')
            db.commit()
            Management(db,'owner').save('',True,sid)
            db.close()
            db=service.database(path)
            self.assertEqual(Sessions(db,'owner').get(sid)['saved'],1)
            db.close()


class StatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_bypasses_active_model_and_does_not_create_history(self):
        db=service.database(':memory:')
        cfg={'FEISHU_OWNER_CHAT_ID':'oc_owner','FEISHU_OWNER_OPEN_ID':'ou_owner',
             'AGENT_HISTORY_TURNS':10,'LARK_CLI_BIN':'fake','AGENT_BACKEND':'codex',
             '_runtime':{'started':time.monotonic(),'listener':'监听就绪'}}
        started,release,status_done=asyncio.Event(),asyncio.Event(),asyncio.Event()
        event={'chat_type':'p2p','sender_type':'user','sender_id':'ou_owner','chat_id':'oc_owner','message_type':'text'}
        service.enqueue(db,cfg,{**event,'message_id':'om_task','content':'排查构建'})
        db.execute("UPDATE inbox SET acknowledged=1 WHERE id='om_task'")
        db.commit()
        async def answer(*args,**kwargs):
            started.set()
            await release.wait()
            return json.dumps({'reply':'结果','session':{'title':'排查','summary':'完成','status':'已完成','next_step':''}})
        async def send(args,*a,**kw):
            body=args[args.index('--markdown')+1]
            if '会话：共' in body:
                self.assertIn('执行任务 #1',body)
                self.assertIn('排队 1 条',body)
                self.assertIn('监听就绪',body)
                status_done.set()
            return '{"ok":true,"data":{"message_id":"om_reply"}}'
        with patch.object(service,'answer',answer),patch.object(service,'run_command',send):
            normal=asyncio.create_task(service.worker(cfg,db))
            fast=asyncio.create_task(service.worker(cfg,db,status_only=True))
            try:
                await asyncio.wait_for(started.wait(),2)
                service.enqueue(db,cfg,{**event,'message_id':'om_queued','content':'稍后再处理'})
                service.enqueue(db,cfg,{**event,'message_id':'om_status','content':' /STATUS '})
                await asyncio.wait_for(status_done.wait(),2)
                self.assertFalse(release.is_set())
                self.assertEqual(db.execute("SELECT state FROM inbox WHERE id='om_status'").fetchone()[0],'done')
                self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT count(*) FROM session_history').fetchone()[0],0)
            finally:
                normal.cancel(); fast.cancel()
                await asyncio.gather(normal,fast,return_exceptions=True)
                db.close()

    async def test_deleted_thread_does_not_restore_history(self):
        from assistant.sessions import ThreadError
        db=service.database(':memory:')
        s=Sessions(db,'owner')
        old=s.create('om_old')
        s.bind(old, 'om_old', 'omt_old')
        Management(db,'owner').delete(old)
        db.commit()
        with self.assertRaises(ThreadError):
            s.route('om_new','继续','omt_old','om_old')
        self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0],0)
        db.close()
