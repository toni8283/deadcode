# module.py — exports api_func via __all__, unused_func is private
__all__ = ["api_func"]


def api_func():
    """Exported via __all__ — must never be PROVABLE."""
    pass


def unused_func():
    """Not exported, not referenced — PROVABLE."""
    pass
