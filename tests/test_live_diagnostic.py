"""Privilege dropping must work on CentOS Python 2.6 and fail closed."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
import diagnose_live_archive as diagnostic


class DiagnosticPrivilegeTests(unittest.TestCase):
    def setUp(self):
        self.account = SimpleNamespace(pw_name='segio', pw_uid=501, pw_gid=502)
        self.actions = Mock()

    def fake_os(self, native=False):
        values = dict(setgroups=self.actions.setgroups, setgid=self.actions.setgid,
                      setuid=self.actions.setuid)
        if native:
            values['initgroups'] = self.actions.initgroups
        return SimpleNamespace(**values)

    def test_python26_fallback_replaces_root_groups_with_employee_groups(self):
        groups = SimpleNamespace(getgrall=Mock(return_value=[
            SimpleNamespace(gr_gid=0, gr_mem=['root']),
            SimpleNamespace(gr_gid=502, gr_mem=['segio']),
            SimpleNamespace(gr_gid=503, gr_mem=['segio', 'other']),
            SimpleNamespace(gr_gid=504, gr_mem=['other'])]))
        with patch.object(diagnostic, 'os', self.fake_os()), patch.dict(sys.modules, {'grp': groups}):
            diagnostic.drop_to_web_account(self.account)
        self.assertEqual(self.actions.mock_calls,
                         [call.setgroups([502, 503]), call.setgid(502), call.setuid(501)])

    def test_native_initgroups_is_preferred_when_available(self):
        with patch.object(diagnostic, 'os', self.fake_os(native=True)):
            diagnostic.drop_to_web_account(self.account)
        self.assertEqual(self.actions.mock_calls,
                         [call.initgroups('segio', 502), call.setgid(502), call.setuid(501)])

    def test_group_initialization_failure_aborts_before_uid_change(self):
        groups = SimpleNamespace(getgrall=Mock(side_effect=OSError('group lookup failed')))
        with patch.object(diagnostic, 'os', self.fake_os()), patch.dict(sys.modules, {'grp': groups}):
            with self.assertRaises(OSError):
                diagnostic.drop_to_web_account(self.account)
        self.assertEqual(self.actions.mock_calls, [])

    def test_setgroups_failure_is_not_ignored(self):
        groups = SimpleNamespace(getgrall=Mock(return_value=[]))
        self.actions.setgroups.side_effect = PermissionError('cannot replace groups')
        with patch.object(diagnostic, 'os', self.fake_os()), patch.dict(sys.modules, {'grp': groups}):
            with self.assertRaises(PermissionError):
                diagnostic.drop_to_web_account(self.account)
        self.assertEqual(self.actions.mock_calls, [call.setgroups([502])])


if __name__ == '__main__':
    unittest.main()
