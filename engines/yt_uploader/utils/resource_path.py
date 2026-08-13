from config.paths import resource_path


def get_appdata_path():
    from config.paths import APPDATA_DIR, ensure_directories

    ensure_directories()
    return APPDATA_DIR
