# openfprintd/polkit.py
from gi.repository import GLib, Gio
import logging

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
            logging.warning(f"Polkit {status} action '{action_id}' for {sender_dbus_name}")
            raise PermissionError(f"Not authorized: {status}")

        logging.info(f"Polkit authorized '{action_id}' for {sender_dbus_name}")
        return True

    except Exception as e:
        # Check if it was a timeout specifically to provide a better log message
        if "Timeout" in str(e):
            logging.error(f"Polkit check timed out for '{action_id}'. Did the user take too long to type?")
        else:
            logging.error(f"Polkit check failed: {e}")

        raise PermissionError("Authorization check failed")
