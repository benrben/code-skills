from __future__ import annotations

import dataclasses
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "code-discipline" / "scripts" / "quality_update.py"
if str(SCRIPT.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT.parent))


def load_module():
    spec = importlib.util.spec_from_file_location("quality_update_restore_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load quality updater")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


update = load_module()


@dataclasses.dataclass
class Child:
    value: int


@dataclasses.dataclass
class Container:
    children: list[Child]
    pair: tuple[Child, ...]
    optional: Child | None


class RestoreValueTests(unittest.TestCase):
    def test_restores_nested_dataclass_collections_and_union(self):
        restored = update.restore_value(
            Container,
            {
                "children": [{"value": 1}],
                "pair": [{"value": 2}],
                "optional": {"value": 3},
            },
        )

        self.assertEqual(restored.children, [Child(1)])
        self.assertEqual(restored.pair, (Child(2),))
        self.assertEqual(restored.optional, Child(3))

    def test_restores_none_and_scalar_values(self):
        self.assertIsNone(update.restore_value(Child | None, None))
        self.assertEqual(update.restore_value(int, 4), 4)


if __name__ == "__main__":
    unittest.main()
