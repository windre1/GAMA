import sys
import os

def get_exe_dir():
    """
    Dapatkan direktori tempat EXE berada (mode portable).
    - Saat berjalan sebagai EXE (frozen): direktori file .exe
    - Saat berjalan sebagai script dev: direktori kerja saat ini
    """
    if getattr(sys, 'frozen', False):
        # sys.executable = path ke file .exe
        return os.path.dirname(os.path.abspath(sys.executable))
    else:
        return os.path.abspath(".")

def resource_path(relative_path):
    """
    Dapatkan path absolut ke resource/aset (read-only).
    Saat di-bundle PyInstaller, aset ada di _MEIPASS atau _internal.
    """
    if getattr(sys, 'frozen', False):
        # PyInstaller menyimpan aset di _MEIPASS
        base_path = sys._MEIPASS
        # PyInstaller 6+ kadang meletakkan di _internal
        internal_path = os.path.join(base_path, "_internal", relative_path)
        if os.path.exists(internal_path):
            return internal_path
    else:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)

def get_writable_path(relative_path):
    """
    Dapatkan path absolut ke lokasi data yang bisa ditulis (PORTABLE).
    Data selalu disimpan di folder yang sama dengan EXE / folder project,
    sehingga aplikasi bisa dipindah ke drive/folder manapun tanpa instalasi.
    """
    base_dir = get_exe_dir()
    full_path = os.path.join(base_dir, relative_path)
    # Pastikan direktori parent sudah ada
    parent_dir = os.path.dirname(full_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    return full_path
