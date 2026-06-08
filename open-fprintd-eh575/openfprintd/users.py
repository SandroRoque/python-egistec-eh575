import pwd


class PermissionError(Exception):
    """Raised when a non-root user tries to operate on another user."""


def resolve_username(bus, sender, requested_username=None):
    """Resolve a username from a D-Bus sender.

    Validates that the sender is either the requested user or root.
    Returns the resolved username.

    Raises PermissionError if a non-root user requests a different user.
    """
    uid = bus.get_unix_user(sender)
    pw = pwd.getpwuid(uid)

    if requested_username is None or requested_username == '':
        return pw.pw_name

    if requested_username != pw.pw_name and uid != 0:
        raise PermissionError(
            f"User {pw.pw_name} cannot operate on {requested_username}"
        )

    return requested_username
