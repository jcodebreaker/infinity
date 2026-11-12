"""The host's index service: one background thread per runner, and the handlers' way to it.

It is a service of the runner process (not a daemon of its own): it starts with the runner in a
managed host, reconciles the files area with the index, then waits for a nudge (an upload, a
saved artifact) or a slow tick and indexes whatever is due. Tool handlers reach it through
:func:`get_runtime`. The index lives under the home volume (``~/.superchat/knowledge``), so it
moves with
the volume.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

from omnigent.superchat.knowledge.embedder import Embedder, ProxyEmbedder
from omnigent.superchat.knowledge.indexer import Indexer
from omnigent.superchat.knowledge.rerank import ProxyReranker, Reranker

_logger = logging.getLogger(__name__)

#: Longest wait between two looks at the disk (a file the Muse wrote by hand is noticed by then).
IDLE_SECONDS = 30.0
HOME_ENV = "SUPERCHAT_KNOWLEDGE_HOME"
ROOT_ENV = "SUPERCHAT_KNOWLEDGE_ROOT"


def default_home() -> Path:
    """``~/.superchat/knowledge`` unless ``SUPERCHAT_KNOWLEDGE_HOME`` says otherwise."""
    return Path(os.environ.get(HOME_ENV) or Path.home() / ".superchat" / "knowledge")


def default_workspace() -> Path:
    """``~/workspace`` unless ``SUPERCHAT_KNOWLEDGE_ROOT`` says otherwise."""
    return Path(os.environ.get(ROOT_ENV) or Path.home() / "workspace")


class KnowledgeRuntime:
    """Owns the indexer and the thread that keeps it current."""

    def __init__(
        self,
        home: Path,
        workspace: Path,
        embedder: Embedder,
        *,
        reranker: Reranker | None = None,
        idle_seconds: float = IDLE_SECONDS,
    ) -> None:
        self.indexer = Indexer(home, workspace, embedder, reranker=reranker)
        self._idle = idle_seconds
        self._wake = threading.Event()
        self._stop = self.indexer.stop_event
        self.indexer.on_pending = self.kick
        self._thread: threading.Thread | None = None
        self.scanned = threading.Event()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Start the thread (once)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="superchat-knowledge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the thread to end and wait for it."""
        self.indexer.shutdown()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def kick(self) -> None:
        """Look at the disk and the queue now."""
        self._wake.set()

    def add(self, path: str | Path, *, explicit: bool = False) -> bool:
        """Queue one file right away (an upload, a saved artifact) and wake the thread."""
        try:
            queued = self.indexer.add(path, explicit=explicit)
        except Exception:  # an event hook must never fail its caller
            _logger.warning("knowledge: could not queue %s", path, exc_info=True)
            return False
        if queued:
            self.kick()
        return queued

    # ------------------------------------------------------------------ the loop

    def _run(self) -> None:
        first = True
        while not self._stop.is_set():
            try:
                self.indexer.scan()
                if first:
                    first = False
                    self.scanned.set()
                while not self._stop.is_set() and self.indexer.process_next():
                    pass
                self.indexer.maybe_reindex()
            except Exception:  # the service must outlive a bad pass
                _logger.warning("knowledge: indexing pass failed", exc_info=True)
            self._wake.wait(timeout=self._idle)
            self._wake.clear()


_runtime: KnowledgeRuntime | None = None
_server_url: str | None = None
_lock = threading.Lock()


def configure(server_url: str | None) -> None:
    """Tell the service where the engine is (for the model proxy)."""
    global _server_url
    if server_url:
        _server_url = server_url


def _credential() -> str | None:
    from omnigent.host.model_credential import read_credential

    return read_credential()


def proxy_access() -> tuple[str | None, str | None]:
    """The engine URL and launch token the model proxy is reached with (``None`` when unknown)."""
    return _server_url, _credential()


def get_runtime(factory: Callable[[], KnowledgeRuntime] | None = None) -> KnowledgeRuntime:
    """The runner's service, built on first use (not started)."""
    global _runtime
    with _lock:
        if _runtime is None:
            _runtime = (factory or _build)()
        return _runtime


def _build() -> KnowledgeRuntime:
    embedder = ProxyEmbedder(lambda: _server_url, _credential)
    reranker = ProxyReranker(lambda: _server_url, _credential)
    return KnowledgeRuntime(default_home(), default_workspace(), embedder, reranker=reranker)


def start_for_runner(server_url: str | None) -> KnowledgeRuntime | None:
    """Start the service when this runner is a managed host (``IS_SANDBOX=1``)."""
    configure(server_url)
    if os.environ.get("IS_SANDBOX") != "1":
        return None
    runtime = get_runtime()
    runtime.start()
    return runtime


def notify_written(path: str | Path) -> None:
    """A file was written through the runner (an upload): index it if the service is running."""
    runtime = _runtime
    if runtime is None and os.environ.get("IS_SANDBOX") == "1":
        # The first upload can land before any tool call built the service: build it now so the
        # file is indexed at once, not on the next slow tick.
        runtime = get_runtime()
        runtime.start()
    if runtime is not None:
        runtime.add(path)


def notify_saved(path: str | Path) -> None:
    """The Muse saved a file as an artifact: keep it searchable even outside the files area."""
    runtime = _runtime
    if runtime is not None:
        runtime.add(path, explicit=True)


def reset_runtime() -> None:
    """Stop and forget the service (tests)."""
    global _runtime, _server_url
    with _lock:
        if _runtime is not None:
            _runtime.stop()
        _runtime = None
        _server_url = None
