"""NFC 测试公共配置。

把 ``mofox`` 仓库根加入 ``sys.path``，让插件的 ``from src.kernel.llm import ...``
能在独立 ``pytest`` 调用时也被解析到。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_PLUGINS_DIR = _PLUGIN_DIR.parent
_REPO_ROOT = _PLUGINS_DIR.parent

for path in (_REPO_ROOT, _PLUGINS_DIR):
    s = str(path)
    if s not in sys.path:
        sys.path.insert(0, s)
