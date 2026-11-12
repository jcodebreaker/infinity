"""Host status route: whether a session's host is up (``/sessions/{id}/host``).

Mounted on the sessions router; the host itself (runner, files, headless browser) is not this
package's concern, only reporting its readiness and launch progress.
"""
