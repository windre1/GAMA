# rthook_neonize.py — Runtime hook PyInstaller untuk neonize.
#
# Masalah: neonize/_binder.py menyimpan `root_dir` saat module di-import:
#     root_dir = os.path.abspath(os.path.dirname(__file__))
# Saat frozen, __file__ menunjuk ke path di dalam zip/temp PyInstaller,
# BUKAN folder di sebelah GAMA.exe. Akibatnya ctypes.CDLL() gagal menemukan
# neonize-windows-amd64.dll yang sudah kita bundel di folder exe.
#
# Fix: patch neonize._binder.root_dir ke sys._MEIPASS (folder unpack PyInstaller)
# yang berisi neonize/ subdirektori dengan DLL-nya — SEBELUM kode apapun
# memanggil load_goneonize().

import os
import sys

if getattr(sys, 'frozen', False):
    try:
        import neonize._binder as _nb
        # DLL dibundel ke neonize/ di dalam _MEIPASS oleh collect_all('neonize')
        _nb.root_dir = os.path.join(sys._MEIPASS, 'neonize')
    except Exception:
        pass
