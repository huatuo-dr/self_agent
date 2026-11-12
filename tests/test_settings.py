import unittest

from assistant import settings


class SettingsTests(unittest.TestCase):
    def test_whitespace_identity_falls_back_and_cannot_disable_signature(self):
        for profile, expected in (({'nickname': '   ', 'name': ' 用户甲 '}, '用户甲'),
                                  ({'nickname': '   ', 'name': '  '}, '用户'),
                                  ({'nickname': '   '}, '用户')):
            cfg = {'MESSAGE_SIGNATURE_TEMPLATE': '{owner_name}', 'OWNER_PROFILE': profile}
            with self.subTest(profile=profile):
                self.assertEqual(settings.message_signature(cfg), expected)
                self.assertEqual(settings.sign_message('正文', cfg), '正文\n\n' + expected)

    def test_empty_rendered_signature_is_rejected(self):
        from unittest.mock import patch
        with patch.object(settings, 'bot_name', return_value=' '):
            with self.assertRaises(ValueError):
                settings.message_signature({'MESSAGE_SIGNATURE_TEMPLATE': '{bot_name}'})

    def test_signature_uses_configured_identity_and_is_idempotent(self):
        cfg = {'BOT_NAME': '小助手', 'OWNER_PROFILE': {'nickname': '主人甲'},
               'MESSAGE_SIGNATURE_TEMPLATE': '{bot_name}代发，{owner_name}授权'}
        self.assertEqual(settings.message_signature(cfg), '小助手代发，主人甲授权')
        text = settings.sign_message('收到', cfg)
        self.assertEqual(text, '收到\n\n小助手代发，主人甲授权')
        self.assertEqual(settings.sign_message(text, cfg), text)
        self.assertEqual(settings.message_signature({}), 'AI 助手代发')

    def test_template_rejects_attributes_unknown_fields_and_formatting(self):
        for template in ('{bot_name.__class__}', '{unknown}', '{bot_name!r}', '{bot_name:>20}'):
            with self.subTest(template=template), self.assertRaises(ValueError):
                settings.message_signature({'MESSAGE_SIGNATURE_TEMPLATE': template})

    def test_timezone_is_explicit_and_invalid_zone_rejected(self):
        self.assertEqual(settings.get_timezone({'TIMEZONE': 'UTC'}).key, 'UTC')
        self.assertEqual(settings.get_timezone().key, 'Asia/Shanghai')
        with self.assertRaises(ValueError):
            settings.get_timezone({'TIMEZONE': 'invalid/zone'})

    def test_bot_names_and_mention_matching(self):
        cfg = {'BOT_NAME': '小助手'}
        self.assertEqual(settings.strip_bot_mention('@小助手 同意', cfg), '同意')
        self.assertEqual(settings.strip_bot_mention('@其他人 同意', cfg), '@其他人 同意')
        with self.assertRaises(ValueError):
            settings.bot_name({'BOT_NAME': '名称\n注入'})
