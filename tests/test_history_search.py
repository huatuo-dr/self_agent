import unittest

from assistant.history_search import PAGE_SIZE, format_result, parse_argument, search
from assistant.management import Management
from assistant.service import database
from assistant.sessions import Sessions


class HistorySearchTests(unittest.TestCase):
    def setUp(self):
        self.db = database(':memory:')
        self.sessions = Sessions(self.db, 'oc_owner')

    def tearDown(self):
        self.db.close()

    def make_session(self, source, title, request, answer='完成', binding='oc_owner'):
        sessions = Sessions(self.db, binding)
        sid = sessions.create(source, title)
        sessions.bind(sid, source, 'omt_' + source, 'https://applink.feishu.cn/example/' + source)
        sessions.finish(sid, source, request, answer)
        self.db.commit()
        return sid

    def test_searches_full_turns_but_only_in_bound_active_sessions(self):
        wanted = self.make_session('om_repository', '仓库排障', '请检查 示例仓库 的 存储')
        other = self.make_session('om_other', '其他人的任务', '示例仓库 私密内容', binding='oc_other')
        self.make_session('om_percent', '100% 覆盖率', '覆盖率统计')

        result = search(self.db, 'oc_owner', '示例仓库')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['id'], wanted)
        self.assertNotEqual(result['items'][0]['id'], other)
        self.assertIn('示例仓库', result['items'][0]['excerpt'])
        self.assertIn('进入原话题', format_result(result))
        self.assertEqual(search(self.db, 'oc_owner', '%')['total'], 1)

        Management(self.db, 'oc_owner').delete(wanted)
        self.db.commit()
        self.assertEqual(search(self.db, 'oc_owner', '示例仓库')['total'], 0)

    def test_pagination_and_input_limits(self):
        for index in range(PAGE_SIZE + 2):
            self.make_session(f'om_{index}', f'项目 {index}', '需要回顾固件构建')
        first = search(self.db, 'oc_owner', '固件')
        second = search(self.db, 'oc_owner', '固件', 2)
        self.assertEqual(first['total'], PAGE_SIZE + 2)
        self.assertEqual(len(first['items']), PAGE_SIZE)
        self.assertEqual(len(second['items']), 2)
        self.assertEqual({item['id'] for item in first['items']} & {item['id'] for item in second['items']}, set())
        self.assertEqual(parse_argument('固件构建 --page 2'), ('固件构建', 2))
        with self.assertRaises(ValueError):
            parse_argument('固件 --page 0')
        with self.assertRaises(ValueError):
            search(self.db, 'oc_owner', 'x' * 101)


if __name__ == '__main__':
    unittest.main()
