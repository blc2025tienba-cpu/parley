import json
import tempfile
import unittest
from pathlib import Path

from _util import temporary_directory
from parley.channel import Channel


class TestChannel(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.ch = Channel(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_emit_increments_and_persists(self):
        a = self.ch.emit("phase_start", phase=12)
        b = self.ch.emit("dispatch", phase=12, turn=1, slice="s1")
        self.assertEqual((a["id"], b["id"]), (1, 2))
        self.assertIn("ts", a)
        # new Channel on same dir continues id sequence
        c = Channel(self.tmp.name).emit("report", phase=12, turn=1, slice="s1")
        self.assertEqual(c["id"], 3)

    def test_update_status_atomic_readback(self):
        self.ch.update_status({"state": "running", "current": {"turn": 5}})
        data = json.loads((Path(self.tmp.name) / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(data["current"]["turn"], 5)
        self.assertFalse((Path(self.tmp.name) / "status.json.tmp").exists())

    def test_read_control(self):
        self.assertIsNone(self.ch.read_control())
        (Path(self.tmp.name) / "control.json").write_text(
            json.dumps({"seq": 3, "verdict": "pause"}), encoding="utf-8")
        self.assertEqual(self.ch.read_control()["seq"], 3)

    def test_snapshot_report(self):
        src = Path(self.tmp.name) / "r.md"
        src.write_text("# report body", encoding="utf-8")
        snap = self.ch.snapshot_report(str(src))
        self.assertTrue(Path(snap.path).exists())
        self.assertEqual(len(snap.sha), 64)
        self.assertEqual(Path(snap.path).read_text(encoding="utf-8"), "# report body")

    def test_resume_pending_dispatch(self):
        self.ch.emit("phase_start", phase=12)
        self.ch.emit("dispatch", phase=12, turn=1, slice="s1")  # no report after -> pending
        st = Channel(self.tmp.name).resume()
        self.assertEqual(st.phase, 12)
        self.assertTrue(st.pending_dispatch)
        # add the report -> no longer pending
        self.ch.emit("report", phase=12, turn=1, slice="s1")
        self.assertFalse(Channel(self.tmp.name).resume().pending_dispatch)


if __name__ == "__main__":
    unittest.main()
