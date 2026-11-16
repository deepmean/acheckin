import unittest
from unittest.mock import Mock, patch
import checkin_dgb as dgb

class SessionTests(unittest.TestCase):
    def test_session_limit_is_actionable_and_not_retried(self):
        s = Mock()
        s.post.return_value.json.return_value = {'success': False, 'code': 'AUTH_SESSION_LIMIT', 'message': 'Conflict'}
        with patch.object(dgb.time, 'sleep'), self.assertRaisesRegex(RuntimeError, 'AUTH_SESSION_LIMIT'):
            dgb.login(s, 'test-user', 'test-password')
        self.assertEqual(s.post.call_count, 1)

    def test_logout_on_success_and_exception(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                s = Mock()
                s.post.return_value.json.return_value = {'success': True}
                response = {'data': {'stats': {'checked_in_today': True}}}
                with patch.object(dgb, 'new_session', return_value=s), patch.object(dgb, 'login', return_value='test-token'), patch.object(dgb, 'get_json', side_effect=RuntimeError('test-error') if fail else None, return_value=response):
                    if fail:
                        with self.assertRaises(RuntimeError):
                            dgb.run_account({'username':'test-user','password':'test-password'}, '2026-11-16', [])
                    else:
                        self.assertTrue(dgb.run_account({'username':'test-user','password':'test-password'}, '2026-11-16', [])['success'])
                self.assertEqual(s.post.call_args.args[0], dgb.BASE + '/api/user/auth/logout')
                s.close.assert_called_once()

if __name__ == '__main__':
    unittest.main()
