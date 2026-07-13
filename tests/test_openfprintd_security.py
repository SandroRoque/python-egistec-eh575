import types
import unittest
from unittest import mock

from openfprintd import users
from openfprintd.device import AlreadyInUse, ClaimDevice, Device, PermissionDenied
from openfprintd.manager import Manager
from openfprintd.polkit import AuthExecutor


class FakeWatcher:
    def __init__(self):
        self.canceled = False

    def cancel(self):
        self.canceled = True


class FakeConnection:
    def __init__(self):
        self.watchers = []

    def watch_name_owner(self, sender, callback):
        watcher = FakeWatcher()
        self.watchers.append((sender, callback, watcher))
        return watcher


class DeviceHarness(Device):
    def __init__(self):
        self._connection = FakeConnection()
        self.bus = mock.Mock()
        self.target = mock.Mock()
        self.owner_watcher = None
        self.claimed_by = None
        self.claim_sender = None
        self.busy = False
        self.busy_operation = None
        self.suspended = False

    @property
    def connection(self):
        return self._connection


class UsernameResolutionTests(unittest.TestCase):
    def test_empty_request_resolves_to_sender(self):
        bus = mock.Mock()
        bus.get_unix_user.return_value = 1000
        account = types.SimpleNamespace(pw_name="alice")
        with mock.patch("openfprintd.users.pwd.getpwuid", return_value=account):
            self.assertEqual(users.resolve_username(bus, ":1.5", ""), "alice")

    def test_non_root_cannot_request_another_user(self):
        bus = mock.Mock()
        bus.get_unix_user.return_value = 1000
        account = types.SimpleNamespace(pw_name="alice")
        with mock.patch("openfprintd.users.pwd.getpwuid", return_value=account):
            with self.assertRaises(users.PermissionError):
                users.resolve_username(bus, ":1.5", "bob")

    def test_root_can_request_another_user(self):
        bus = mock.Mock()
        bus.get_unix_user.return_value = 0
        account = types.SimpleNamespace(pw_name="root")
        with mock.patch("openfprintd.users.pwd.getpwuid", return_value=account):
            self.assertEqual(users.resolve_username(bus, ":1.1", "alice"), "alice")


class DeviceOwnershipTests(unittest.TestCase):
    def test_claim_and_release_are_bound_to_sender(self):
        device = DeviceHarness()
        with mock.patch("openfprintd.device.users.resolve_username", return_value="alice"):
            device.Claim("alice", sender=":1.5", connection=None)

        self.assertEqual(device.claimed_by, "alice")
        self.assertEqual(device.claim_sender, ":1.5")
        with self.assertRaises(ClaimDevice):
            device.Release(sender=":1.6", connection=None)

        watcher = device.owner_watcher
        device.Release(sender=":1.5", connection=None)
        self.assertTrue(watcher.canceled)
        self.assertIsNone(device.claimed_by)

    def test_second_claim_is_rejected(self):
        device = DeviceHarness()
        with mock.patch("openfprintd.device.users.resolve_username", return_value="alice"):
            device.Claim("alice", sender=":1.5", connection=None)
            with self.assertRaises(AlreadyInUse):
                device.Claim("alice", sender=":1.6", connection=None)

    def test_suspend_preserves_claim_and_forwards_lifecycle(self):
        device = DeviceHarness()
        device.owner_watcher = FakeWatcher()
        device.claimed_by = "alice"
        device.claim_sender = ":1.5"

        device.Suspend()

        self.assertTrue(device.suspended)
        self.assertEqual(device.claimed_by, "alice")
        device.target.Suspend.assert_called_once_with()

        device.Resume()
        self.assertFalse(device.suspended)
        device.target.Resume.assert_called_once_with()


class ManagerRegistrationTests(unittest.TestCase):
    def _manager(self):
        manager = Manager.__new__(Manager)
        manager.devices = {}
        return manager

    def test_registration_requires_polkit(self):
        manager = self._manager()
        with mock.patch(
            "openfprintd.manager.polkit.check_privilege",
            side_effect=PermissionError,
        ):
            with self.assertRaises(PermissionDenied):
                manager.RegisterDevice("/backend", sender=":1.5", connection=None)
        self.assertEqual(manager.devices, {})

    def test_authorized_registration_binds_backend_sender(self):
        manager = self._manager()
        wrapper = mock.Mock()
        with mock.patch("openfprintd.manager.polkit.check_privilege"), mock.patch(
            "openfprintd.manager.Device",
            return_value=wrapper,
        ):
            manager.RegisterDevice("/backend", sender=":1.5", connection=None)

        self.assertIs(manager.devices["/backend"], wrapper)
        wrapper.set_target.assert_called_once_with("/backend", ":1.5")


class AuthorizationExecutorTests(unittest.TestCase):
    def test_denied_authorization_never_runs_operation(self):
        executor = AuthExecutor(max_workers=1, max_pending=1)
        success = mock.Mock()
        failure = mock.Mock()
        operation = mock.Mock()

        with mock.patch(
            "openfprintd.polkit.check_privilege",
            side_effect=PermissionError,
        ), mock.patch(
            "openfprintd.polkit.GLib.idle_add",
            side_effect=lambda callback, *args: callback(*args),
        ), mock.patch.object(
            executor._executor,
            "submit",
            side_effect=lambda callback: callback(),
        ):
            executor.authorize(":1.5", "test.action", success, failure, operation)

        operation.assert_not_called()
        success.assert_not_called()
        failure.assert_called_once()
        executor._executor.shutdown(wait=False)


if __name__ == "__main__":
    unittest.main()
