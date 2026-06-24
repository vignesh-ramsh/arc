"""
plugins.admin.routes
====================
Privileged admin routes, auto-discovered and imported by relay (it scans every
plugin's ``routes/*.py``). Every handler's first line is ``require_admin(ctx)``.

All paths are absolute and live under ``/api/v1/admin/*`` so they ride relay's
sub-app (and therefore authn's before_req authenticator) while staying clear of
the ``/admin`` static mount that serves the UI.
"""
