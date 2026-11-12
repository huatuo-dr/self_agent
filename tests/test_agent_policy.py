import tempfile
import unittest
from pathlib import Path

from assistant.agent_policy import policy_for


class AgentPolicyTests(unittest.TestCase):
    def test_readonly_skill_directory_must_be_dedicated_temporary_input(self):
        with tempfile.TemporaryDirectory(prefix='skill-input-') as directory:
            path = Path(directory)
            policy = policy_for({}, 'chat', self.root, read_only_dirs=(path,))
            self.assertEqual(policy.read_only_dirs, (path,))
        for path in (Path(tempfile.gettempdir()), self.root, self.root / '.local'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                policy_for({}, 'chat', self.root, read_only_dirs=(path,))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.project').mkdir()

    def test_every_task_uses_explicit_shared_policy(self):
        for kind in ('chat', 'classify', 'auto_query', 'report', 'develop_discuss'):
            with self.subTest(kind=kind):
                policy = policy_for({}, kind, self.root)
                self.assertEqual(policy.kind, kind)
                self.assertFalse(policy.source_writable)
        with self.assertRaises(ValueError):
            policy_for({}, 'develop_execute', self.root)
        self.assertTrue(policy_for({}, 'develop_execute', self.root,
                                   approval_ref='server-approved-42').source_writable)
        with self.assertRaises(ValueError):
            policy_for({}, 'unknown', self.root)

    def test_rejects_broad_sensitive_and_symlink_grants(self):
        for path in (self.root, self.root / '.project', self.root / '.local',
                     self.root / '.project/credential', self.root / '.project/reports'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                policy_for({}, 'report', self.root, writable_roots=(path,))
        with tempfile.TemporaryDirectory() as elsewhere:
            (self.root / '.project/link').symlink_to(elsewhere, target_is_directory=True)
            with self.assertRaises(ValueError):
                policy_for({}, 'report', self.root,
                           writable_roots=(self.root / '.project/link',))
        granted = self.root / '.project/repo'
        granted.mkdir()
        self.assertEqual(policy_for({}, 'report', self.root,
                                    writable_roots=(granted,)).writable_roots, (granted,))

    def test_input_files_cannot_authorize_sensitive_data(self):
        for name in ('.env', '.local/data', '.project/credential/key'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture')
            with self.subTest(name=name), self.assertRaises(ValueError):
                policy_for({}, 'chat', self.root, input_files=(path,))

    def test_network_endpoint_is_selected_backend_only(self):
        cfg = {'AGENT_BACKEND': 'grok', 'GROK_BASE_URL': 'http://model.fixture:8123/v1',
               'CODEX_BASE_URL': 'https://unused.fixture',
               'AGENT_NETWORK_ALLOWLIST': 'extra.fixture:8443'}
        policy = policy_for(cfg, 'chat', self.root)
        self.assertIn(('model.fixture', 8123), policy.network_allowlist)
        self.assertIn(('extra.fixture', 8443), policy.network_allowlist)
        self.assertNotIn(('unused.fixture', 443), policy.network_allowlist)

    def test_parent_traversal_grant_is_rejected(self):
        with self.assertRaises(ValueError):
            policy_for({}, 'report', self.root,
                       writable_roots=(self.root / '.project/repo/../../outside',))

    def test_input_symlink_is_rejected(self):
        file = self.root / 'fixture.txt'
        file.write_text('fixture')
        alias = self.root / 'alias.txt'
        alias.symlink_to(file)
        with self.assertRaises(ValueError):
            policy_for({}, 'chat', self.root, input_files=(alias,))

    def test_hardlinked_input_is_not_an_auth_exception(self):
        import os
        secret = self.root / '.env'
        secret.write_text('sensitive-fixture')
        disguised = self.root / 'ordinary.txt'
        os.link(secret, disguised)
        with self.assertRaises(ValueError):
            policy_for({}, 'chat', self.root, input_files=(disguised,))

    def test_discussion_and_inference_cannot_receive_write_grants(self):
        repo = self.root / '.project/repo'
        for kind in ('develop_discuss', 'classify', 'auto_query'):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                policy_for({}, kind, self.root, writable_roots=(repo,))

    def test_custom_owner_profile_is_never_a_model_input(self):
        profile = self.root / 'identity.json'
        profile.write_text('owner-private-fixture')
        cfg = {'OWNER_PROFILE_PATH': 'identity.json'}
        with self.assertRaises(ValueError):
            policy_for(cfg, 'chat', self.root, input_files=(profile,))
