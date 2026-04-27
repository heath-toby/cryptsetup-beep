"""PC speaker test for the wizard's Page 1.

We don't try to detect whether the hardware actually has a beeper — most modern
laptops don't. Instead we run beep(1), and the user confirms whether they heard
anything on Page 2. If beep(1) isn't installed we report that clearly so the
wizard can skip straight to ALSA.
"""

from __future__ import annotations

import shutil
import subprocess


class PcSpkrUnavailable(Exception):
    """beep(1) isn't installed."""


def test(frequency: int = 440, length_ms: int = 500) -> None:
    """Make a noise (or try to). Raises PcSpkrUnavailable if beep is missing."""
    if shutil.which("beep") is None:
        raise PcSpkrUnavailable("the 'beep' package is not installed")
    subprocess.run(
        ["beep", "-f", str(frequency), "-l", str(length_ms)],
        check=False,  # exit code may be non-zero on hardware without a beeper
    )
