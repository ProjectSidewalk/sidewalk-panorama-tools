"""Run pip with setuptools' distutils shim loaded first (r4_pip22.sh): pip 22.0.2 imports distutils.cmd, which
this WSL lacks (no python3-distutils, no sudo). A file, not `python -c`, because the shim reads __file__."""
import sys

import setuptools  # noqa: F401  (installs the distutils shim)
from pip._internal.cli.main import main

sys.exit(main())
