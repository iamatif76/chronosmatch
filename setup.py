"""
Setup script for compiling ChronosMatch Cython extension.
"""

import sys
import os
from setuptools import setup, Extension

try:
    from Cython.Build import cythonize
    USE_CYTHON = True
except ImportError:
    USE_CYTHON = False

include_dirs = [os.path.join(os.path.dirname(__file__), "engine")]

if USE_CYTHON:
    ext_modules = cythonize(
        [
            Extension(
                "engine.matching_engine",
                sources=[
                    "engine/matching_engine.pyx",
                    "engine/native_engine.c",
                ],
                include_dirs=include_dirs,
                extra_compile_args=["-O3"] if sys.platform != "win32" else ["/O2"],
            )
        ],
        compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            "wraparound": False,
            "cdivision": True,
            "nonecheck": False,
        },
    )
else:
    ext_modules = []

setup(
    name="chronosmatch",
    version="0.1.0",
    description="Zero-Copy High-Frequency Trading Matching Engine",
    ext_modules=ext_modules,
)
