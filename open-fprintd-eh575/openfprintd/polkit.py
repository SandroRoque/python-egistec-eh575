# openfprintd/polkit.py
import concurrent.futures
import logging
import threading

from gi.repository import GLib, Gio

logger = logging.getLogger("POLKIT")


def check_privilege(sender_dbus_name, action_id):
    """
    Checks if the D-Bus sender is authorized for the given PolicyKit action.
    Increased timeout to allow for user password entry.
    """
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

        authority = Gio.DBusProxy.new_sync(
            bus,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.freedesktop.PolicyKit1",
            "/org/freedesktop/PolicyKit1/Authority",
            "org.freedesktop.PolicyKit1.Authority",
            None,
        )

        subject_value = (
            "system-bus-name",
            {"name": GLib.Variant("s", sender_dbus_name)}
        )

        # Flags: 1 = Allow User Interaction
        parameters = GLib.Variant(
            "((sa{sv})sa{ss}us)",
            (subject_value, action_id, {}, 1, "")
        )

        # Increase timeout to 300,000ms (5 minutes) to allow time for the password prompt
        result = authority.call_sync(
            "CheckAuthorization",
            parameters,
            Gio.DBusCallFlags.NONE,
            300000,
            None
        )

        result_tuple = result.unpack()
        struct_val = result_tuple[0]
        (is_auth, is_challenge, _) = struct_val

        if not is_auth:
            # If it's not authorized but 'is_challenge' is true, it means the user
            # cancelled the password dialog.
            status = "Dismissed" if is_challenge else "Denied"
            logger.warning("Polkit %s action '%s' for %s", status, action_id, sender_dbus_name)
            raise PermissionError(f"Not authorized: {status}")

        logger.info("Polkit authorized '%s' for %s", action_id, sender_dbus_name)
        return True

    except Exception as e:
        # Check if it was a timeout specifically to provide a better log message
        if "Timeout" in str(e):
            logger.error("Polkit check timed out for '%s'. Did the user take too long to type?", action_id)
        else:
            logger.error("Polkit check failed: %s", e)

        raise PermissionError("Authorization check failed")


class AuthExecutor:
    """Runs PolKit authorization checks in a thread pool, invoking
    callbacks on the GLib main loop so D-Bus methods remain responsive."""

    def __init__(self, max_workers=8, max_pending=16):
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        self._semaphore = threading.BoundedSemaphore(max_pending)

    def authorize(self, sender, action_id, success_cb, error_cb, operation_cb):
        """Check PolKit auth in a thread, then run *operation_cb* on success.

        *operation_cb* is called on the GLib main thread and may return a
        value which is passed to *success_cb*.
        """
        if not self._semaphore.acquire(blocking=False):
            GLib.idle_add(error_cb, PermissionError("Too many pending auth checks"))
            return

        def _auth_thread():
            try:
                check_privilege(sender, action_id)
                GLib.idle_add(_run_op)
            except Exception:
                GLib.idle_add(error_cb, PermissionError("Not authorized"))
            finally:
                self._semaphore.release()

        def _run_op():
            try:
                result = operation_cb()
                if result is not None:
                    success_cb(result)
                else:
                    success_cb()
            except Exception as e:
                error_cb(e)

        self._executor.submit(_auth_thread)
