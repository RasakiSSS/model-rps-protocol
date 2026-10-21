"""Guard the module layering: lower layers must not import from higher ones."""
import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / 'src' / 'model_rps'

# Subpackages are the top layers. The flat modules form the core.
# Allowed direction: mcp_agent -> room -> core. Core never imports a subpackage.
SUBPACKAGES = ('room', 'mcp_agent')
ALLOWED = {
    'room': {'room'},
    'mcp_agent': {'mcp_agent', 'room'},
}

# Core modules that legitimately bridge into the upper layers (entry points).
CORE_ENTRY_POINTS = {'cli.py', '__main__.py'}


def imported_modules(path: Path) -> list[str]:
    """Return absolute dotted names of in-package imports found in a file."""
    relative = path.relative_to(PACKAGE).with_suffix('')
    package_parts = ['model_rps', *relative.parts[:-1]]
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.ImportFrom) and node.level:
            base = package_parts[:len(package_parts) - node.level + 1]
            found.append('.'.join(base + ([node.module] if node.module else [])))
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('model_rps'):
            found.append(node.module)
        elif isinstance(node, ast.Import):
            found.extend(a.name for a in node.names if a.name.startswith('model_rps'))
    return found


def top_level(dotted: str) -> str | None:
    parts = dotted.split('.')
    return parts[1] if len(parts) > 1 else None


class ArchitectureTests(unittest.TestCase):
    def test_core_does_not_import_subpackages(self):
        for path in PACKAGE.glob('*.py'):
            if path.name in CORE_ENTRY_POINTS:
                continue
            for name in imported_modules(path):
                self.assertNotIn(top_level(name), SUBPACKAGES, f'{path.name} imports {name}')

    def test_subpackages_follow_allowed_direction(self):
        for package, allowed in ALLOWED.items():
            for path in (PACKAGE / package).glob('*.py'):
                for name in imported_modules(path):
                    target = top_level(name)
                    if target in SUBPACKAGES:
                        self.assertIn(target, allowed, f'{package}/{path.name} imports {name}')

    def test_room_does_not_depend_on_mcp(self):
        for path in (PACKAGE / 'room').glob('*.py'):
            for name in imported_modules(path):
                self.assertNotEqual(top_level(name), 'mcp_agent', f'room/{path.name} imports {name}')

    def test_no_import_cycles_between_subpackages(self):
        edges = {p: set() for p in SUBPACKAGES}
        for package in SUBPACKAGES:
            for path in (PACKAGE / package).glob('*.py'):
                for name in imported_modules(path):
                    target = top_level(name)
                    if target in SUBPACKAGES and target != package:
                        edges[package].add(target)
        for a, targets in edges.items():
            for b in targets:
                self.assertNotIn(a, edges[b], f'cycle between {a} and {b}')


if __name__ == '__main__':
    unittest.main()
