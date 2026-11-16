"""Tests read the deployment config through load_config(). When no config is
selected (HPM_CONFIG unset) and the checked-in example still holds its
placeholders, use a copy with a dummy account so the unit tests run; the
template tests then skip until that config has been synthesized."""
import atexit
import json
import os
import shutil
import tempfile

from config import DEFAULT_CONFIG, _placeholders

if not os.environ.get("HPM_CONFIG") and _placeholders(json.loads(DEFAULT_CONFIG.read_text())):
    _folder = tempfile.mkdtemp(prefix="hpm-test-config-")
    atexit.register(shutil.rmtree, _folder, ignore_errors=True)
    _text = DEFAULT_CONFIG.read_text().replace("<your-account-id>", "123456789012")
    os.environ["HPM_CONFIG"] = os.path.join(_folder, "example.json")
    with open(os.environ["HPM_CONFIG"], "w") as _handle:
        _handle.write(_text)
