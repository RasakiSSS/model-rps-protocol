"""Exercise the Windows entry point without network or local model credentials."""

import base64
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which('powershell.exe') if os.name == 'nt' else None


@unittest.skipUnless(POWERSHELL, 'Windows PowerShell is required')
class LauncherTests(unittest.TestCase):
    def launch(self, script, args, directory):
        return subprocess.run(
            [POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script), *args],
            cwd=directory, capture_output=True, text=True, encoding='utf-8', timeout=20,
        )

    def test_help_from_another_directory_ignores_config_and_restores_location(self):
        with tempfile.TemporaryDirectory() as directory:
            # A caller's config must not be loaded by the project launcher.
            (Path(directory) / 'config.toml').write_text('not valid TOML', encoding='utf-8')
            script = str(ROOT / 'run.ps1').replace("'", "''")
            command = (
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                f"& '{script}' --help; $status = $LASTEXITCODE; "
                "Write-Output ('CALLER=' + (Get-Location).Path); exit $status"
            )
            encoded = base64.b64encode(command.encode('utf-16-le')).decode('ascii')
            result = subprocess.run(
                [POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded],
                cwd=directory, capture_output=True, text=True, encoding='utf-8', timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--red-profile', result.stdout)
            self.assertIn('CALLER=' + directory, result.stdout)

    def test_demo_and_quoted_paths_work_from_outside_project(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory) / '日志 with spaces'
            result = self.launch(ROOT / 'run.ps1', [
                '--no-config', '--mode', 'demo', '--bo', '3',
                '--log-dir', str(logs),
            ], directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('BO3', result.stdout)
            runs = list(logs.iterdir())
            self.assertEqual(len(runs), 1)
            self.assertTrue((runs[0] / 'chat.log').is_file())
            # No default config is created as a side effect.
            self.assertFalse((Path(directory) / 'config.toml').exists())

    def test_nonzero_game_and_configuration_exit_codes_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            draw = self.launch(ROOT / 'run.ps1', [
                '--no-config', '--mode', 'demo', '--scenario', 'draw',
                '--max-games', '1', '--log-dir', str(Path(directory) / 'logs'),
            ], directory)
            self.assertEqual(draw.returncode, 3, draw.stderr)
            invalid = self.launch(ROOT / 'run.ps1', ['--bo', 'not-an-integer'], directory)
            self.assertEqual(invalid.returncode, 2, invalid.stderr)

    def test_missing_environment_explains_setup_without_falling_back(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'run.ps1'
            shutil.copyfile(ROOT / 'run.ps1', script)
            result = self.launch(script, ['--help'], directory)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('Python environment not found', result.stderr)
            self.assertIn('-m venv .venv', result.stderr)
            self.assertIn('-m pip install -e .', result.stderr)
            self.assertFalse((Path(directory) / '.venv').exists())


if __name__ == '__main__':
    unittest.main()
