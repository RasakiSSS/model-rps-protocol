"""Guard the module layering: a package may only import from layers below it."""
import ast
import importlib.util
import re
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / 'src' / 'model_rps'

# Lowest layer first. A subpackage may import itself and anything earlier in this list.
LAYERS = ['core', 'llm', 'players', 'engine', 'ui', 'room', 'mcp_agent']

# Top-level modules outside the layers. They assemble layers and may import any of them.
# Layers must not import them, except for the explicit exceptions below.
ROOT_MODULES = {'cli', 'config', 'player_factory', '__main__'}
ALLOWED_ROOT_IMPORTS = {
    'room': {'config'},
    'mcp_agent': {'cli', 'config', 'player_factory'},
}


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


def target_of(dotted: str) -> str | None:
    parts = dotted.split('.')
    return parts[1] if len(parts) > 1 else None


class ArchitectureTests(unittest.TestCase):
    def test_layer_directories_match_declared_layers(self):
        packages = {p.name for p in PACKAGE.iterdir() if p.is_dir() and (p / '__init__.py').exists()}
        self.assertEqual(packages, set(LAYERS))

    def test_no_loose_modules_beyond_entry_points(self):
        loose = {p.stem for p in PACKAGE.glob('*.py')} - {'__init__'}
        self.assertEqual(loose, ROOT_MODULES)

    def test_layers_only_import_downward(self):
        for layer in LAYERS:
            allowed = set(LAYERS[:LAYERS.index(layer) + 1])
            for path in (PACKAGE / layer).glob('*.py'):
                for name in imported_modules(path):
                    target = target_of(name)
                    if target in LAYERS:
                        self.assertIn(target, allowed, f'{layer}/{path.name} imports upward: {name}')

    def test_layers_use_root_modules_only_when_listed(self):
        for layer in LAYERS:
            allowed = ALLOWED_ROOT_IMPORTS.get(layer, set())
            for path in (PACKAGE / layer).glob('*.py'):
                for name in imported_modules(path):
                    target = target_of(name)
                    if target in ROOT_MODULES:
                        self.assertIn(target, allowed, f'{layer}/{path.name} imports root module: {name}')

    def test_root_modules_do_not_depend_on_upper_layers_they_should_not_know(self):
        # config and player_factory are shared building blocks; only entry points may reach room/mcp_agent.
        for name in ('config', 'player_factory'):
            for imported in imported_modules(PACKAGE / f'{name}.py'):
                self.assertNotIn(target_of(imported), {'room', 'mcp_agent', 'ui', 'engine'},
                                 f'{name}.py imports {imported}')

    def test_dotted_module_strings_point_to_real_modules(self):
        """String references such as `-m model_rps.x` bypass import checks, so verify them separately."""
        pattern = re.compile(r'\bmodel_rps(?:\.[A-Za-z_]\w*)+')
        sources = [*PACKAGE.rglob('*.py'), *(PACKAGE.parents[1] / 'examples').glob('*.yml'),
                   *PACKAGE.parents[1].glob('*.md'), *(PACKAGE.parents[1] / 'docs').rglob('*.md')]
        for path in sources:
            for name in set(pattern.findall(path.read_text(encoding='utf-8'))):
                # Accept a module path followed by attribute names, e.g. model_rps.cli.main.
                parts = name.split('.')
                for end in range(len(parts), 1, -1):
                    try:
                        if importlib.util.find_spec('.'.join(parts[:end])) is not None:
                            break
                    except (ImportError, ValueError):
                        continue
                else:
                    self.fail(f'{path.name} references unknown module {name}')


if __name__ == '__main__':
    unittest.main()
