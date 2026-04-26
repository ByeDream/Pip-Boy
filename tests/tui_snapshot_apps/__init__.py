"""Driver scripts for ``pytest-textual-snapshot`` baselines.

Each module here exposes ``app: PipBoyTuiApp`` so the snapshot fixture
can ``import`` and run the scenario headlessly. Keeping them in a
package (rather than inline strings inside the test file) makes
re-running ``pytest --snapshot-update`` straightforward.
"""

from __future__ import annotations
