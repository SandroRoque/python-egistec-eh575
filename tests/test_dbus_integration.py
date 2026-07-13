import os
import shutil
import signal
import subprocess
import threading
import unittest
from unittest import mock

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

from openfprintd.manager import Manager


class FakeBackend(dbus.service.Object):
    INTERFACE = "io.github.uunicorn.Fprint.Device"

    @dbus.service.signal(INTERFACE, signature="sb")
    def VerifyStatus(self, result, done):
        pass

    @dbus.service.signal(INTERFACE, signature="s")
    def VerifyFingerSelected(self, finger):
        pass

    @dbus.service.signal(INTERFACE, signature="sb")
    def EnrollStatus(self, result, done):
        pass


@unittest.skipUnless(shutil.which("dbus-daemon"), "dbus-daemon is unavailable")
class DbusManagerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            result = subprocess.run(
                [
                    "dbus-daemon", "--session", "--fork",
                    "--print-address=1", "--print-pid=1",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as error:
            if os.environ.get("EGIS_REQUIRE_DBUS_TEST") == "1":
                raise
            raise unittest.SkipTest(
                f"private D-Bus daemon cannot start: {error.stderr.strip()}"
            ) from error
        lines = result.stdout.strip().splitlines()
        cls.address = lines[0]
        cls.daemon_pid = int(lines[1])
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    @classmethod
    def tearDownClass(cls):
        os.kill(cls.daemon_pid, signal.SIGTERM)

    def test_backend_registration_reaches_standard_fprint_manager(self):
        server_bus = dbus.bus.BusConnection(self.address)
        backend_bus = dbus.bus.BusConnection(self.address)
        client_bus = dbus.bus.BusConnection(self.address)
        server_name = dbus.service.BusName("net.reactivated.Fprint", server_bus)
        backend_name = dbus.service.BusName(
            "io.github.uunicorn.Fprint.Device.Egis",
            backend_bus,
        )
        manager = Manager(server_name)
        backend_path = "/org/reactivated/Fprint/Device/Egis"
        backend = FakeBackend(backend_name, backend_path)
        loop = GLib.MainLoop()
        loop_thread = threading.Thread(target=loop.run, daemon=True)
        loop_thread.start()
        try:
            manager_proxy = backend_bus.get_object(
                "net.reactivated.Fprint",
                "/net/reactivated/Fprint/Manager",
            )
            manager_interface = dbus.Interface(
                manager_proxy,
                "net.reactivated.Fprint.Manager",
            )
            with mock.patch("openfprintd.manager.polkit.check_privilege"):
                manager_interface.RegisterDevice(dbus.ObjectPath(backend_path))

            client_proxy = client_bus.get_object(
                "net.reactivated.Fprint",
                "/net/reactivated/Fprint/Manager",
            )
            client_interface = dbus.Interface(
                client_proxy,
                "net.reactivated.Fprint.Manager",
            )
            self.assertEqual(
                str(client_interface.GetDefaultDevice()),
                "/net/reactivated/Fprint/Device/0",
            )
        finally:
            loop.quit()
            loop_thread.join(timeout=2)
            backend.remove_from_connection()
            manager.remove_from_connection()
            server_bus.close()
            backend_bus.close()
            client_bus.close()


if __name__ == "__main__":
    unittest.main()
