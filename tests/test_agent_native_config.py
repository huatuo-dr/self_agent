import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from assistant.agent_native_config import native_config


class NativeConfigTests(unittest.TestCase):
    def setUp(self):
        # Every setting in this suite comes from a fixture, never the host's
        # authentication/model environment (including assertion diagnostics).
        environment = patch.dict('os.environ', {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_process_environment_precedes_native_auth_endpoint_and_model(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            path.write_text(json.dumps({'env': {'ANTHROPIC_AUTH_TOKEN': 'native-token',
                'ANTHROPIC_BASE_URL': 'https://native.example'}, 'model': 'native-model'}))
            with patch.dict('os.environ', {'ANTHROPIC_AUTH_TOKEN': 'process-token',
                    'ANTHROPIC_BASE_URL': 'https://process.example', 'ANTHROPIC_MODEL': 'process-model'}):
                cfg = native_config({'AGENT_BACKEND': 'claudecode', 'CLAUDECODE_SETTINGS_FILE': str(path)})
            self.assertEqual(cfg['ANTHROPIC_AUTH_TOKEN'], 'process-token')
            self.assertEqual(cfg['ANTHROPIC_BASE_URL'], 'https://process.example')
            self.assertEqual(cfg['CLAUDECODE_MODEL'], 'process-model')

    def test_explicit_anthropic_model_precedes_native_model(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            path.write_text('{"model":"native-model"}')
            cfg=native_config({'AGENT_BACKEND':'claudecode','CLAUDECODE_SETTINGS_FILE':str(path),
                               'ANTHROPIC_MODEL':'configured-model'})
            self.assertEqual(cfg['CLAUDECODE_MODEL'],'configured-model')

    def test_configured_claude_home_precedes_environment_home(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory)/'configured';home.mkdir()
            (home/'settings.json').write_text('{"env":{"ANTHROPIC_AUTH_TOKEN":"configured-home-token"}}')
            with patch.dict('os.environ',{'CLAUDE_CONFIG_DIR':str(Path(directory)/'other')}):
                cfg=native_config({'AGENT_BACKEND':'claudecode','CLAUDE_CONFIG_DIR':str(home)})
            self.assertEqual(cfg['ANTHROPIC_AUTH_TOKEN'],'configured-home-token')

    def test_claude_preserves_only_native_auth_endpoint_and_model(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            path.write_text(json.dumps({'env':{'ANTHROPIC_AUTH_TOKEN':'fixture-token',
                'ANTHROPIC_BASE_URL':'https://model.example','OTHER_SECRET':'hidden'},
                'model':'fixture-model','hooks':{'bad':'never execute'},'sandbox':{'enabled':True}}))
            cfg=native_config({'AGENT_BACKEND':'claudecode','CLAUDECODE_SETTINGS_FILE':str(path)})
        self.assertEqual(cfg['ANTHROPIC_AUTH_TOKEN'],'fixture-token')
        self.assertEqual(cfg['ANTHROPIC_BASE_URL'],'https://model.example')
        self.assertEqual(cfg['CLAUDECODE_MODEL'],'fixture-model')
        self.assertNotIn('OTHER_SECRET',cfg)
        self.assertNotIn('hooks',cfg)
        self.assertNotIn('sandbox',cfg)

    def test_explicit_config_overrides_native_values_and_unselected_cli_is_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json';path.write_text('{"env":{"ANTHROPIC_AUTH_TOKEN":"old"}}')
            with patch.dict('os.environ', {'ANTHROPIC_AUTH_TOKEN': 'process-token'}):
                cfg=native_config({'AGENT_BACKEND':'claudecode','CLAUDECODE_SETTINGS_FILE':str(path),
                                   'ANTHROPIC_AUTH_TOKEN':'configured'})
            self.assertEqual(cfg['ANTHROPIC_AUTH_TOKEN'],'configured')
        with patch('pathlib.Path.read_text',side_effect=AssertionError('unselected settings read')):
            self.assertEqual(native_config({'AGENT_BACKEND':'grok'})['AGENT_BACKEND'],'grok')

    def test_managed_native_sandbox_is_not_silently_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'managed-settings.json';path.write_text('{"sandbox":{"enabled":true}}')
            with patch('assistant.agent_native_config.MANAGED_FILES',{'claudecode':(path,)}):
                with self.assertRaisesRegex(ValueError,'沙箱'):
                    native_config({'AGENT_BACKEND':'claudecode','CLAUDECODE_SETTINGS_FILE':str(Path(directory)/'missing')})
