import json
import tempfile
import unittest
from pathlib import Path

from _util import temporary_directory
from parley import cli


class TestInit(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.addCleanup(self.tmp.cleanup)
        self.proj = Path(self.tmp.name) / "proj"
        self.proj.mkdir()
        (self.proj / "package.json").write_text("{}", encoding="utf-8")
        (self.proj / "pnpm-lock.yaml").write_text("", encoding="utf-8")
        (self.proj / "Cargo.toml").write_text("[package]", encoding="utf-8")
        self.cfgp = str(Path(self.tmp.name) / "parley.config.json")
        self.data = str(Path(self.tmp.name) / "data")

    def test_init_detects_syncs_writes(self):
        cfg = cli.init(str(self.proj), goal="do x", config_path=self.cfgp, data_dir=self.data)
        gates = cfg["verify"]["gates"]
        self.assertIn(["cargo", "test"], gates)
        self.assertTrue(any(g[0] == "pnpm" for g in gates))
        self.assertTrue(Path(self.cfgp).exists())
        # agents synced from canonical repo .kiro/agents
        self.assertTrue((self.proj / ".kiro" / "agents" / "coder.json").exists())
        # gitignore updated
        self.assertIn(".kiro/agents/", (self.proj / ".gitignore").read_text(encoding="utf-8"))
        self.assertTrue(cfg["roles"]["coder"]["edit"])

    def test_idempotent_keeps_goal(self):
        cli.init(str(self.proj), goal="original", config_path=self.cfgp, data_dir=self.data)
        cli.init(str(self.proj), goal=None, config_path=self.cfgp, data_dir=self.data)
        self.assertEqual(json.loads(Path(self.cfgp).read_text(encoding="utf-8"))["goal"], "original")

    def test_load_config_roundtrip(self):
        cli.init(str(self.proj), goal="g", config_path=self.cfgp, data_dir=self.data)
        cfg, goal, phase, contract = cli.load_config(self.cfgp)
        self.assertEqual(goal, "g")
        self.assertTrue(cfg.roles["coder"].edit)
        self.assertFalse(cfg.roles["analyzer"].edit)


if __name__ == "__main__":
    unittest.main()
