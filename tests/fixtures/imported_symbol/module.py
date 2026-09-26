# imported_symbol.py – tests that an imported name used in the same file
# is referenced.  build_path and check_path are also called at module level.

from os.path import join, exists


def build_path(base, name):
    return join(base, name)


def check_path(p):
    return exists(p)


# Call both functions so they have at least one reference
_result1 = build_path("/tmp", "file.txt")
_result2 = check_path("/tmp/file.txt")
