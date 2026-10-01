"""Tests for the plugin. Run from the repo root: python3 -m unittest discover -s tests -t .

They are not part of the installed plugin, so they put the plugin directory on
the import path themselves.
"""

import os
import sys

PLUGIN_ROOT = os.path.join(os.path.dirname(__file__), "..", "plugins", "agent-techport-sources")
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
sys.path.insert(0, os.path.abspath(PLUGIN_ROOT))
