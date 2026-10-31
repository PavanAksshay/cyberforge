"""Windows system commit headroom, distinct from free pagefile storage."""

import ctypes
import os
from functools import lru_cache


class MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32),
        ("load", ctypes.c_uint32),
        *(
            (name, ctypes.c_uint64)
            for name in (
                "total_physical",
                "available_physical",
                "total_pagefile",
                "available_pagefile",
                "total_virtual",
                "available_virtual",
                "available_extended_virtual",
            )
        ),
    ]


@lru_cache
def memory_status_api():
    function = ctypes.WinDLL("kernel32", use_last_error=True).GlobalMemoryStatusEx
    function.argtypes = [ctypes.POINTER(MemoryStatus)]
    function.restype = ctypes.c_int
    return function


def system_commit_available():
    if os.name != "nt":
        return None
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not memory_status_api()(ctypes.byref(status)):
        raise ctypes.WinError(ctypes.get_last_error())
    return status.available_pagefile
