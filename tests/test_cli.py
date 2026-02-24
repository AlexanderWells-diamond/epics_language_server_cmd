import subprocess
import sys

from epics_cmd_language_server import __version__


def test_cli_version():
    cmd = [sys.executable, "-m", "epics_cmd_language_server", "--version"]
    assert subprocess.check_output(cmd).decode().strip() == __version__
