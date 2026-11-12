"""The warm deck browser: a job attaches to it in a context of its own, else goes cold.

Offline: the CDP endpoint is a fake client and Chromium is a fake process.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
loader = importlib.machinery.SourceFileLoader("superchat_deck_export_warm", str(HERE.parent / "bin" / "superchat-deck-export"))
spec = importlib.util.spec_from_loader(loader.name, loader)
helper = importlib.util.module_from_spec(spec)
loader.exec_module(helper)


class FakeClient:
    """Records CDP calls and answers the few the job's setup and teardown make."""

    calls: list = []

    def __init__(self, ws_url):
        self.ws_url = ws_url
        self.closed = False

    def connect(self):
        FakeClient.calls.append(("connect", self.ws_url))

    def close(self):
        FakeClient.calls.append(("close", self.ws_url))

    def call(self, method, params=None, session_id=None, timeout_s=60.0):
        FakeClient.calls.append((method, params))
        if method == "Target.createBrowserContext":
            return {"browserContextId": "ctx-1"}
        if method == "Target.createTarget":
            return {"targetId": "tgt-1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "sess-1"}
        return {}


class FakeCdp:
    """The warm port: served after ``fails`` refused probes, never when ``fails`` is None."""

    def __init__(self, fails=0):
        self.fails = fails
        self.probes = 0
        self.CdpClient = FakeClient

    def discover_ws_url(self, port):
        self.probes += 1
        if self.fails is None or self.probes <= self.fails:
            raise OSError("connection refused")
        return f"ws://127.0.0.1:{port}/devtools/browser/x"


def no_launch(*_a, **_k):
    raise AssertionError("no Chromium may be launched here")


class WarmBrowserTest(unittest.TestCase):
    def setUp(self):
        FakeClient.calls = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = Path(self.tmp.name) / "deck-browser"
        for name, value in (("WARM_PROFILE", self.profile), ("WARM_PORT", 9333)):
            patcher = patch.object(helper, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        sleep = patch.object(helper.time, "sleep", lambda _s: None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_a_job_attaches_to_the_warm_browser_in_a_context_of_its_own_and_closes_it(self):
        cdp = FakeCdp()
        with patch.object(helper, "load_cdp_module", lambda: cdp), patch.object(
            helper.subprocess, "Popen", no_launch
        ), helper.Chromium() as browser:
            self.assertTrue(browser.warm)
            self.assertEqual(browser.session, "sess-1")
        self.assertEqual(cdp.probes, 1)
        methods = [c[0] for c in FakeClient.calls]
        self.assertIn(("Target.createBrowserContext", {"disposeOnDetach": True}), FakeClient.calls)
        self.assertIn(
            ("Target.createTarget", {"url": "about:blank", "browserContextId": "ctx-1"}),
            FakeClient.calls,
        )
        self.assertLess(methods.index("Target.closeTarget"), methods.index("Target.disposeBrowserContext"))
        self.assertEqual(methods[-1], "close")

    def test_while_the_host_runs_a_warm_browser_a_job_waits_for_it_instead_of_going_cold(self):
        self.profile.mkdir()
        cdp = FakeCdp(fails=3)
        with patch.object(helper, "load_cdp_module", lambda: cdp), patch.object(
            helper.subprocess, "Popen", no_launch
        ), helper.Chromium() as browser:
            self.assertTrue(browser.warm)
        self.assertEqual(cdp.probes, 4)

    def test_without_a_warm_browser_on_this_host_the_job_launches_its_own_and_says_so(self):
        launched = []

        def cold(self):
            launched.append(True)
            self._connect("ws://127.0.0.1:4444/devtools/browser/cold")

        err = io.StringIO()
        with patch.object(helper, "load_cdp_module", lambda: FakeCdp(fails=None)), patch.object(
            helper.Chromium, "_launch_cold", cold
        ), contextlib.redirect_stderr(err), helper.Chromium() as browser:
            self.assertFalse(browser.warm)
        self.assertEqual(launched, [True])
        self.assertIn("no warm browser on port 9333; launching a cold one", err.getvalue())
        self.assertNotIn("Target.createBrowserContext", [c[0] for c in FakeClient.calls])

    def test_the_export_alarm_during_the_wait_ends_the_job_without_a_cold_launch(self):
        self.profile.mkdir()

        class Alarmed(FakeCdp):
            def discover_ws_url(self, port):
                raise helper.ExportError("the export took too long", "TIMEOUT")

        with patch.object(helper, "load_cdp_module", Alarmed), patch.object(
            helper.Chromium, "_launch_cold", no_launch
        ), self.assertRaises(helper.ExportError) as caught, helper.Chromium():
            pass
        self.assertEqual(caught.exception.code, "TIMEOUT")


class ServeTest(unittest.TestCase):
    def test_serve_leaves_an_already_served_port_alone(self):
        with patch.object(helper, "load_cdp_module", FakeCdp), patch.object(helper, "launch_chromium", no_launch):
            helper.serve()

    def test_serve_runs_one_chromium_on_the_warm_port_until_it_exits(self):
        class Proc:
            pid = 4242

            def wait(self, timeout=None):
                return 0

        launched, stopped = [], []
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "deck-browser"
            (profile / "Default").mkdir(parents=True)
            with patch.object(helper, "WARM_PROFILE", profile), patch.object(
                helper, "load_cdp_module", lambda: FakeCdp(fails=None)
            ), patch.object(helper, "launch_chromium", lambda *a: launched.append(a) or Proc()), patch.object(
                helper, "stop_chromium", stopped.append
            ), contextlib.redirect_stderr(io.StringIO()):
                helper.serve()
            self.assertTrue((profile / "Default").is_dir(), "the profile is never wiped")
        self.assertEqual(launched, [(profile, helper.WARM_PORT)])
        self.assertEqual(len(stopped), 1)


if __name__ == "__main__":
    unittest.main()
