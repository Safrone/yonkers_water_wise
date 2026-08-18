"""Shared test configuration.

Tests import the integration through its real package path,
`custom_components.yonkers_waterwise`, which pytest resolves because
`pythonpath = ["."]` puts the repository root on `sys.path`.

pytest-homeassistant-custom-component registers itself as an entry-point
plugin, so it must not be listed in `pytest_plugins` as well.

Note there is deliberately no autouse `enable_custom_integrations` fixture here.
That fixture depends on `hass`, and requesting `hass` before `recorder_mock`
makes the recorder fixture assert. Tests that need Home Assistant to load the
integration from `custom_components/` should request it explicitly, after
`recorder_mock`.
"""

from __future__ import annotations
