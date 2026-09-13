"""Run the documented PowerShell workflow against disposable synthetic storage."""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path


class DocumentedCLITests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "documented shell is PowerShell")
    def test_documented_powershell_workflow(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "references" / "operations.md").read_text(encoding="utf-8")
        body = re.search(r"```powershell\n(.*?)\n```", text, re.S).group(1)
        body = body.replace("$python = 'python'", "$python = '" + sys.executable.replace("'", "''") + "'")
        # The example's own GUID directory is the only cleanup target.
        script = "$ErrorActionPreference = 'Stop'\ntry {\n" + body + "\n} finally {\n" + r"""
if ($root -and (Test-Path -LiteralPath $root)) {
  $resolved = (Resolve-Path -LiteralPath $root).Path
  $temp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
  if (-not $resolved.StartsWith($temp, [StringComparison]::OrdinalIgnoreCase) -or (Split-Path $resolved -Leaf) -notmatch '^memorycore-ai-synthetic-[0-9a-f]{32}$') { throw 'Unsafe cleanup path' }
  Remove-Item -LiteralPath $resolved -Recurse -Force
}
}
"""
        run = subprocess.run(["powershell", "-NoProfile", "-Command", script], cwd=root, capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('"imported":true', run.stdout)
        self.assertIn('"status":"purged"', run.stdout)
        self.assertIn('"status":"consolidated"', run.stdout)


if __name__ == "__main__":
    unittest.main()
