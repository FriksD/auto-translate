# PyInstaller runtime hook: ensure torch/lib is in DLL search path before any import
import os
import sys

_torch_lib = os.path.join(sys._MEIPASS, "torch", "lib")
if os.path.isdir(_torch_lib):
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(_torch_lib)
    os.environ["PATH"] = _torch_lib + os.pathsep + os.environ.get("PATH", "")
