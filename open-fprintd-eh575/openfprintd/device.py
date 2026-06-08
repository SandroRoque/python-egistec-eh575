import dbus
import dbus.service
import concurrent.futures
import logging
import os
import pwd
import threading
from gi.repository import GLib
import openfprintd.polkit as polkit

INTERFACE_NAME = 'net.reactivated.Fprint.Device'
ENROLL_STAGES = 10

class AlreadyInUse(dbus.DBusException):
    _dbus_error_name = 'net.reactivated.Fprint.Error.AlreadyInUse'
    def __init__(self):
        super().__init__('Device is already in use')

class ClaimDevice(dbus.DBusException):
    _dbus_error_name = 'net.reactivated.Fprint.Error.ClaimDevice'
    def __init__(self):
        super().__init__('Client must claim device first')

class PermissionDenied(dbus.DBusException):
    _dbus_error_name = 'net.reactivated.Fprint.Error.PermissionDenied'
    def __init__(self):
        super().__init__('Permission denied')

class Device(dbus.service.Object):
    cnt=0
    _AUTH_MAX_WORKERS = 8
    _auth_executor = concurrent.futures.ThreadPoolExecutor(max_workers=_AUTH_MAX_WORKERS)
    _auth_semaphore = threading.BoundedSemaphore(_AUTH_MAX_WORKERS * 2)

    def __init__(self, mgr):
        self.manager = mgr
        bus_name = mgr.bus_name
        dbus.service.Object.__init__(self, bus_name, '/net/reactivated/Fprint/Device/%d' % Device.cnt)
        Device.cnt += 1
        self.bus = bus_name.get_bus()
        self.target = None
        self.target_props = dbus.Dictionary({
                'name':  'DBus driver',
                'num-enroll-stages': ENROLL_STAGES,
                'scan-type': 'press'
            })
        self.owner_watcher = None
        self.claimed_by = None
        self.claim_sender = None
        self.busy = False
        self.suspended = False
        self.callbacks = []

    # --- Helper: Async Auth Wrapper ---
    def _run_with_auth(self, sender, action, success_cb, error_cb, operation_cb):
        """
        Runs the Polkit check in a thread to avoid blocking the main loop.
        If successful, executes operation_cb() on the main thread.
        Bounded by a thread pool and semaphore to prevent resource exhaustion.
        """
        if not Device._auth_semaphore.acquire(blocking=False):
            GLib.idle_add(error_cb, PermissionDenied())
            return

        def auth_thread():
            try:
                polkit.check_privilege(sender, action)
                GLib.idle_add(run_op)
            except Exception:
                GLib.idle_add(error_cb, PermissionDenied())
            finally:
                Device._auth_semaphore.release()

        def run_op():
            try:
                result = operation_cb()
                if result is not None:
                    success_cb(result)
                else:
                    success_cb()
            except Exception as e:
                error_cb(e)

        Device._auth_executor.submit(auth_thread)

    # --- Standard Methods ---

    def proxy_call(self, cb):
        if self.suspended or self.target is None:
            logging.debug('The service is suspended / offline, delay the call')
            self.callbacks += [cb]
        else:
            cb()

    def call_cbs(self):
        for cb in self.callbacks:
            try: cb()
            except Exception as e: logging.debug('callback error: %s' % repr(e))
        self.suspended = False
        self.callbacks = []

    def set_target(self, target_name, sender):
        self.target = self.bus.get_object(sender, target_name, introspect=False)
        self.target = dbus.Interface(self.target, 'io.github.uunicorn.Fprint.Device')
        self.target.connect_to_signal('VerifyStatus', self.VerifyStatus)
        self.target.connect_to_signal('VerifyFingerSelected', self.VerifyFingerSelected)
        self.target.connect_to_signal('EnrollStatus', self.EnrollStatus)

        watcher = None
        def watch_cb(name):
            if name == '':
                self.unset_target()
                watcher.cancel()
        watcher = self.connection.watch_name_owner(sender, watch_cb)

        def process_offline():
            if not self.suspended:
                self.call_cbs()
        GLib.idle_add(process_offline)

    def unset_target(self):
        self.target = None

    def Resume(self):
        self.suspended = False
        if self.target is not None:
            self.target.Resume()
            self.call_cbs()

    def Suspend(self):
        if self.owner_watcher is not None or self.busy:
            print("[DEVICE] Clearing active fingerprint claim for suspend")
        self.suspended = True
        self.callbacks = []
        self.claimed_by = None
        self.claim_sender = None
        self.busy = False
        if self.owner_watcher is not None:
            self.owner_watcher.cancel()
            self.owner_watcher = None
        if self.target is not None:
            self.target.Suspend()

    # ------------------ Template Database --------------------------

    @dbus.service.method(dbus_interface=INTERFACE_NAME,
                         in_signature="s",
                         out_signature="as",
                         connection_keyword='connection',
                         sender_keyword='sender',
                         async_callbacks=('callback', 'errback'))
    def ListEnrolledFingers(self, username, sender, connection, callback, errback):
        logging.debug('ListEnrolledFingers')
        if username is None or username == '':
            uid=self.bus.get_unix_user(sender)
            pw=pwd.getpwuid(uid)
            username=pw.pw_name
        else:
            uid = self.bus.get_unix_user(sender)
            pw = pwd.getpwuid(uid)
            if username != pw.pw_name and uid != 0:
                 raise PermissionDenied()

        def cb():
            callback(self.target.ListEnrolledFingers(username, signature='s'))
        self.proxy_call(cb)

    @dbus.service.method(dbus_interface=INTERFACE_NAME,
                         in_signature='s',
                         out_signature='',
                         connection_keyword='connection',
                         sender_keyword='sender',
                         async_callbacks=('success_cb', 'error_cb'))
    def DeleteEnrolledFingers(self, username, sender, connection, success_cb, error_cb):
        logging.debug('DeleteEnrolledFingers: %s' % username)

        def op():
            uid = self.bus.get_unix_user(sender)
            pw = pwd.getpwuid(uid)
            target_user = username
            if target_user is None or len(target_user) == 0:
                target_user = pw.pw_name
            elif target_user != pw.pw_name and uid != 0:
                raise PermissionDenied()
            return self.target.DeleteEnrolledFingers(target_user, signature='s')

        self._run_with_auth(sender, "net.reactivated.fprint.device.enroll", success_cb, error_cb, op)

    # ------------------ Claim/Release --------------------------

    @dbus.service.method(dbus_interface=INTERFACE_NAME,
                         in_signature='s',
                         out_signature='',
                         connection_keyword='connection',
                         sender_keyword='sender')
    def Claim(self, username, sender, connection):
        logging.debug('Claim')
        uid=self.bus.get_unix_user(sender)
        pw=pwd.getpwuid(uid)
        if username is None or len(username) == 0:
            username = pw.pw_name
        elif username != pw.pw_name and uid != 0:
            raise PermissionDenied()

        if self.owner_watcher is not None:
            raise AlreadyInUse()

        def watch_cb(x):
            if x == '':
                self.do_release()

        self.owner_watcher = self.connection.watch_name_owner(sender, watch_cb)
        self.claimed_by = username
        self.claim_sender = sender

    @dbus.service.method(dbus_interface=INTERFACE_NAME,
                         in_signature='',
                         out_signature='',
                         connection_keyword='connection',
                         sender_keyword='sender')
    def Release(self, sender, connection):
        logging.debug('Release')
        if self.owner_watcher is None or self.claim_sender != sender:
            raise ClaimDevice()
        self.do_release()

    def do_release(self):
        logging.debug('do_release')
        self.claimed_by = None
        self.claim_sender = None
        if self.owner_watcher is not None:
            self.owner_watcher.cancel()
            self.owner_watcher = None
        if self.busy:
            self.target.Cancel(signature='')
            self.busy = False

    # ------------------ Verify --------------------------

    @dbus.service.method(dbus_interface=INTERFACE_NAME,
                         in_signature='s',
                         out_signature='',
                         connection_keyword='connection',
                         sender_keyword='sender',
                         async_callbacks=('success_cb', 'error_cb'))
    def VerifyStart(self, finger_name, sender, connection, success_cb, error_cb):
        logging.debug('VerifyStart requested')

        def op():
            if self.owner_watcher is None or self.claim_sender != sender:
                raise ClaimDevice()
            self.busy = True
            return self.target.VerifyStart(self.claimed_by, finger_name, signature='ss')

        self._run_with_auth(sender, "net.reactivated.fprint.device.verify", success_cb, error_cb, op)

    @dbus.service.method(dbus_interface=INTERFACE_NAME,
                         in_signature='',
                         out_signature='',
                         connection_keyword='connection',
                         sender_keyword='sender')
    def VerifyStop(self, sender, connection):
        logging.debug('VerifyStop')
        if self.owner_watcher is None or self.claim_sender != sender:
            raise ClaimDevice()
        self.busy = False
        self.target.Cancel(signature='')

    @dbus.service.signal(dbus_interface=INTERFACE_NAME, signature='s')
    def VerifyFingerSelected(self, finger): pass

    @dbus.service.signal(dbus_interface=INTERFACE_NAME, signature='sb')
    def VerifyStatus(self, result, done):
        if done: self.busy = False

    # ------------------ Enroll --------------------------

    @dbus.service.method(dbus_interface=INTERFACE_NAME,
                         in_signature='s',
                         out_signature='',
                         connection_keyword='connection',
                         sender_keyword='sender',
                         async_callbacks=('success_cb', 'error_cb'))
    def EnrollStart(self, finger_name, sender, connection, success_cb, error_cb):
        logging.debug('EnrollStart requested')

        def op():
            if self.owner_watcher is None or self.claim_sender != sender:
                raise ClaimDevice()
            self.busy = True
            return self.target.EnrollStart(self.claimed_by, finger_name, signature='ss')

        self._run_with_auth(sender, "net.reactivated.fprint.device.enroll", success_cb, error_cb, op)

    @dbus.service.method(dbus_interface=INTERFACE_NAME,
                         in_signature='',
                         out_signature='',
                         connection_keyword='connection',
                         sender_keyword='sender')
    def EnrollStop(self, sender, connection):
        logging.debug('EnrollStop')
        if self.owner_watcher is None or self.claim_sender != sender:
            raise ClaimDevice()
        self.busy = False
        self.target.Cancel(signature='')

    @dbus.service.signal(dbus_interface=INTERFACE_NAME, signature='sb')
    def EnrollStatus(self, result, done):
        if done: self.busy = False

    # ------------------ Debug --------------------------

    @dbus.service.method(dbus_interface=INTERFACE_NAME,
                         in_signature='s',
                         out_signature='s',
                         connection_keyword='connection',
                         sender_keyword='sender',
                         async_callbacks=('success_cb', 'error_cb'))
    def RunCmd(self, s, sender, connection, success_cb, error_cb):
        logging.debug('RunCmd')
        if os.environ.get("EGIS_DEBUG") != "1":
            error_cb(PermissionDenied())
            return
        def op():
            if self.target is None:
                logging.debug('No target driver registered for RunCmd')
                return ''
            try:
                return self.target.RunCmd(s, signature='s')
            except dbus.exceptions.DBusException as e:
                if 'UnknownMethod' not in getattr(e, '_dbus_error_name', ''):
                    raise
                logging.debug('Target driver does not support RunCmd')
                return ''
        self._run_with_auth(sender, "net.reactivated.fprint.manager.register", success_cb, error_cb, op)

    # ------------------ Props --------------------------

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='ss', out_signature='v')
    def Get(self, interface, prop):
        logging.debug('Get %s.%s' % (interface, prop))
        return self.GetAll(interface)[prop]

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='ssv')
    def Set(self, interface, prop, value):
        logging.debug('Set %s.%s=%s' % (interface, prop, repr(value)))
        if interface != INTERFACE_NAME:
            raise dbus.exceptions.DBusException('net.reactivated.Fprint.Error.UnknownInterface')
        raise dbus.exceptions.DBusException('net.reactivated.Fprint.Error.NotImplemented')

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        logging.debug('GetAll %s' % (interface))
        if interface != INTERFACE_NAME:
            raise dbus.exceptions.DBusException('net.reactivated.Fprint.Error.UnknownInterface')
        return self.target_props
