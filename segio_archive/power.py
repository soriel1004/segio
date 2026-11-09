"""Keep Windows awake during GUI jobs while allowing display off and locking."""
from contextlib import contextmanager
import os


@contextmanager
def keep_awake(log=lambda _:None):
    setter=None;message=None
    if os.name=='nt':
        try:
            import ctypes
            setter=ctypes.windll.kernel32.SetThreadExecutionState
            setter.argtypes=[ctypes.c_uint];setter.restype=ctypes.c_uint
            if not setter(0x80000001):setter=None
        except (AttributeError,OSError):setter=None
        message='작업 중 자동 절전 방지 적용 · 화면보호기·화면 잠금은 사용 가능합니다.' if setter else '자동 절전 방지를 적용하지 못했습니다. 작업 중 Windows 절전 설정을 확인하세요.'
    try:
        if message:log(message)
        yield
    finally:
        if setter:
            try:setter(0x80000000)
            except OSError:pass
