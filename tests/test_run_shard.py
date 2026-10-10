import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_shard  # noqa: E402


class TestPartition(unittest.TestCase):
    def test_every_module_in_exactly_one_shard(self):
        modules = run_shard.discover_modules()
        self.assertIn("test_run_shard", modules)
        for count in (1, 2, 3, 5, 100):
            shards = run_shard.partition(modules, count)
            self.assertEqual(len(shards), count)
            flat = [m for shard in shards for m in shard]
            self.assertEqual(sorted(flat), modules)
            self.assertEqual(len(flat), len(set(flat)))

    def test_deterministic(self):
        modules = run_shard.discover_modules()
        self.assertEqual(run_shard.partition(modules, 3), run_shard.partition(modules, 3))

    def test_unknown_modules_still_assigned(self):
        shards = run_shard.partition(["a", "b", "c", "d"], 2, weights={"a": 10.0})
        self.assertEqual(sorted(m for s in shards for m in s), ["a", "b", "c", "d"])

    def test_weighted_balance(self):
        w = {"big": 10.0, "x": 5.0, "y": 5.0}
        shards = run_shard.partition(list(w), 2, weights=w)
        self.assertIn(["big"], shards)

    def test_weights_table_names_real_modules(self):
        modules = set(run_shard.discover_modules())
        self.assertLessEqual(set(run_shard.load_weights()), modules)


if __name__ == "__main__":
    unittest.main()
