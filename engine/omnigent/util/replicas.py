"""Whether this deployment runs more than one engine replica."""

from __future__ import annotations

import os


def multi_replica() -> bool:
    """
    Whether more than one engine replica serves this deployment.

    Set ``OMNIGENT_MULTI_REPLICA=1`` when several engines share one database. Off
    (the default, one engine), a host is connected exactly when this process's
    :class:`HostRegistry` holds it: an ``"online"`` hosts row with no connection
    here is left over from a connection that is gone (this engine restarted, the
    host died), never a host held by another replica.

    :returns: ``True`` when the hosts table may describe connections held by other
        replicas.
    """
    return os.environ.get("OMNIGENT_MULTI_REPLICA", "").strip().lower() in {"1", "true", "yes"}
