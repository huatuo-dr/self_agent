from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assistant import service


class BackendConfigurationTests(unittest.TestCase):
    def configured(self, root, name, **options):
        for skill in ('history-search','gitlab-assistant','report-creation','group-message-watch'):
            target=root/'.agents/skills'/skill/'SKILL.md';target.parent.mkdir(parents=True,exist_ok=True);target.write_text('skill')
        (root/'scripts').mkdir()
        for script in ('search_history.py','search_images.py'):(root/'scripts'/script).touch()
        data={'FEISHU_APP_ID':'cli_test','FEISHU_OWNER_OPEN_ID':'ou_owner','FEISHU_OWNER_CHAT_ID':'oc_owner',
              'AGENT_BACKEND':name,**options}
        calls=[]
        selected={'codex':'codex','grok':'grok','claudecode':'claude'}[name]
        def executable(value):
            calls.append(value)
            if value in ('codex','grok','claude') and value!=selected:
                raise AssertionError('unselected executable probed: '+value)
            return '/fake/'+value
        with patch.object(service,'ROOT',root),patch.object(service,'read_env',return_value=data), \
                patch.object(service.shutil,'which',side_effect=executable):
            return service.config(),calls

    def test_all_three_backends_support_reports_group_watch_and_evolution_independently(self):
        for name in ('codex','grok','claudecode'):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as directory:
                cfg,calls=self.configured(Path(directory),name,REPORT_SKILL_ENABLED='true',
                                          GROUP_WATCH_SKILL_ENABLED='true',EVOLUTION_ENABLED='true')
                self.assertEqual(cfg['AGENT_BACKEND'],name)
                self.assertTrue(cfg['REPORT_SKILL_ENABLED'])
                self.assertIn('bwrap',calls)
                self.assertEqual(cfg[name.upper()+'_BIN'], '/fake/'+('claude' if name=='claudecode' else name))

    def test_runtime_maps_are_parsed_as_json_without_touching_other_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg,_=self.configured(Path(directory),'grok',
                AGENT_AUTH_FILES='{"grok":{".grok/auth.json":"/private/example.json"}}')
            self.assertEqual(cfg['AGENT_AUTH_FILES']['grok']['.grok/auth.json'],'/private/example.json')
