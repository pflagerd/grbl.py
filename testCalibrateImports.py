#!/usr/bin/env python3
"""Unit test for calibrate.py's polite message when the venv isn't active.

Runs calibrate.py --help in a child Python where `import serial` is made to
fail (sys.modules['serial'] = None makes it raise ModuleNotFoundError), so the
test gives the same answer whether or not this Python has pyserial.  --help
never reaches a serial port, and with serial blocked none can be opened anyway.

Run: python testCalibrateImports.py
"""

import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

# Block pyserial, then run calibrate.py as if it were the main program.
RUN_WITHOUT_SERIAL = (
    "import runpy, sys\n"
    "sys.modules['serial'] = None\n"
    "sys.argv = ['calibrate.py', '--help']\n"
    "runpy.run_path('calibrate.py', run_name='__main__')\n"
)


class MissingImportTests(unittest.TestCase):
    def setUp(self):
        self.completed = subprocess.run(
            [sys.executable, "-c", RUN_WITHOUT_SERIAL],
            cwd=HERE, capture_output=True, text=True, timeout=30)
        self.output = self.completed.stdout + self.completed.stderr

    def test_message_starts_with_please(self):
        self.assertTrue(self.output.startswith("Please"), self.output)

    def test_message_gives_the_activate_command(self):
        self.assertIn("source .venv/bin/activate", self.output)

    def test_message_names_the_missing_module(self):
        self.assertIn("'serial'", self.output)

    def test_exit_status_is_not_zero(self):
        self.assertNotEqual(self.completed.returncode, 0, self.output)

    def test_no_traceback(self):
        self.assertNotIn("Traceback", self.output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
