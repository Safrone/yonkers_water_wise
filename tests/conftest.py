"""Make the integration's Home Assistant-independent modules importable.

`custom_components/yonkers_waterwise/__init__.py` pulls in Home Assistant, which
is not a test dependency here. Bind the package directory to a private name so
`api.py` and `const.py` can be imported on their own, relative imports included.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

_PKG_DIR = (
    Path(__file__).resolve().parent.parent / "custom_components" / "yonkers_waterwise"
)

if "_yww" not in sys.modules:
    _pkg = types.ModuleType("_yww")
    _pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["_yww"] = _pkg
