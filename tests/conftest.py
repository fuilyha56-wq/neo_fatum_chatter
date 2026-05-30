"""NFC 测试公共配置。"""

from __future__ import annotations

import sys
import types
from pathlib import Path

PACKAGE_NAME = "neo_fatum_chatter"
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = next(parent for parent in PACKAGE_ROOT.parents if (parent / "src").is_dir())

repo_root_path = str(REPO_ROOT)
if repo_root_path not in sys.path:
    sys.path.insert(0, repo_root_path)

for module_name in list(sys.modules):
    if module_name == PACKAGE_NAME or module_name.startswith(f"{PACKAGE_NAME}."):
        sys.modules.pop(module_name, None)

package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(PACKAGE_ROOT)]
package.__file__ = str(PACKAGE_ROOT / "__init__.py")
package.__package__ = PACKAGE_NAME
sys.modules[PACKAGE_NAME] = package
