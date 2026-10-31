"""The extension's process as a node runs it, through the SDK's stand-in node, with a stand-in for what the node asks
core (core.call: the folder's syncs, the Files tab's index): it speaks the typed protocol, watches its folder and
says so, and rescans when asked. Skipped where $POINTMAN_EXTENSIONS_SDK doesn't name the node's Python SDK."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK = os.environ.get("POINTMAN_EXTENSIONS_SDK")
if SDK:
    sys.path.insert(0, SDK)
try:
    import pointman_extension_types as types
    from pointman_extension_testing import StandInNode
except ImportError:
    StandInNode = None


@unittest.skipUnless(StandInNode, "needs the node's Python SDK ($POINTMAN_EXTENSIONS_SDK)")
class FolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = (Path(self.tmp.name) / "youtube").resolve()
        self.root.mkdir()
        self.calls = []

        def core_call(req):
            self.calls.append((req.method, req.path))
            if (req.method, req.path) == ("GET", "/v1/node/syncs"):
                return types.CoreCallResponse(status=200, body=[])
            return types.CoreCallResponse(status=200, body={})

        self.node = StandInNode(ROOT, settings={"root": str(self.root), "keep_awake": "never", "notes_file": "",
                                                "takeover": str(Path(self.tmp.name) / "none.db")},
                                methods={"core.call": core_call}, state_dir=Path(self.tmp.name) / "node",
                                roots=[self.root])
        self.node.start()

    def tearDown(self):
        self.node.stop()
        self.tmp.cleanup()

    def status(self) -> dict:
        for _ in range(200):
            status = self.node.events.get("dailies.status") or {}
            if status.get("last_scan"):
                return status
            time.sleep(0.05)
        self.fail(f"the dailies didn't start:\n{self.node.log}")

    def test_it_speaks_the_typed_protocol_and_watches_its_folder(self):
        self.assertEqual(self.node.info.protocol, types.Protocol.PROTOCOL_TYPED)
        status = self.status()
        self.assertEqual(status["root"], str(self.root))
        self.assertTrue(status["allowed"])

    def test_a_rescan_reads_the_syncs_again(self):
        self.status()
        before = self.calls.count(("GET", "/v1/node/syncs"))
        self.assertEqual(self.node.run("dailies.rescan").result, {"message": "Rescanning"})
        for _ in range(100):
            if self.calls.count(("GET", "/v1/node/syncs")) > before:
                break
            time.sleep(0.05)
        self.assertGreater(self.calls.count(("GET", "/v1/node/syncs")), before)

    def count(self, path: str) -> int:
        return sum(1 for m, p in self.calls if m == "GET" and p.split("?")[0] == path)

    def test_syncs_are_read_as_it_starts_then_only_when_the_node_says_they_changed(self):
        self.status()
        time.sleep(2.5)                                   # the main loop goes round every second: no poll
        self.assertEqual(self.count("/v1/node/syncs"), 1, self.calls)
        self.node.notify(types.SyncsChangedRequest())
        for _ in range(100):
            if self.count("/v1/node/syncs") > 1:
                break
            time.sleep(0.05)
        self.assertEqual(self.count("/v1/node/syncs"), 2, self.calls)


@unittest.skipUnless(StandInNode, "needs the node's Python SDK ($POINTMAN_EXTENSIONS_SDK)")
class NotesTests(unittest.TestCase):
    """Review notes are read as it starts, then only when the node says core's changed (notes.changed)."""

    def test_notes_are_read_when_the_node_says_they_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = (Path(tmp) / "youtube").resolve()
            (root / "loft").mkdir(parents=True)
            calls = []

            def core_call(req):
                calls.append((req.method, req.path.split("?")[0]))
                return types.CoreCallResponse(status=200, body=[])

            node = StandInNode(ROOT, settings={"root": str(root), "keep_awake": "never",
                                               "notes_file": "review-notes.md", "takeover": str(Path(tmp) / "none.db")},
                               methods={"core.call": core_call}, state_dir=Path(tmp) / "node", roots=[root])
            node.start()
            try:
                notes = lambda: calls.count(("GET", "/v1/node/notes"))   # noqa: E731
                for _ in range(100):
                    if notes():
                        break
                    time.sleep(0.05)
                time.sleep(2.5)
                self.assertEqual(notes(), 1, calls)
                node.notify(types.NotesChangedRequest())
                for _ in range(100):
                    if notes() > 1:
                        break
                    time.sleep(0.05)
                self.assertEqual(notes(), 2, calls)
            finally:
                node.stop()


@unittest.skipUnless(StandInNode, "needs the node's Python SDK ($POINTMAN_EXTENSIONS_SDK)")
class PublishTests(unittest.TestCase):
    def test_the_published_file_and_version_are_kept_by_their_numbers(self):
        from pointman_dailies.publisher import Node
        asked = []

        def request(message, timeout):           # files.publish, as the node answers it
            asked.append(message)
            return types.FilesPublishResponse(file=types.FileRef(id="file_2636"),
                                              version=types.FileVersionRef(id="ver_7"))

        self.assertEqual(Node(request).publish({"files": []}), {"asset_id": 2636, "version_id": 7})
        self.assertIsInstance(asked[0], types.FilesPublishRequest)


if __name__ == "__main__":
    unittest.main()
