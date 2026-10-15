"""使用真实 AstrBot SDK 导入插件，不伪造框架 API。"""

import importlib
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "hr_plugin_under_test"
module = types.ModuleType(PACKAGE)
module.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = module


def plugin_module(name):
    return importlib.import_module(f"{PACKAGE}.{name}")
