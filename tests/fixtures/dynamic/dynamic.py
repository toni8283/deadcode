# dynamic.py – ambiguous cases that must be classified as REVIEW.

# Case 1: getattr-based access – dynamic reference
import importlib

def dynamic_loader(name):
    mod = importlib.import_module("mypackage")
    return getattr(mod, name)


# Case 2: __all__ export – may be imported by external consumers
__all__ = ["exported_function"]


def exported_function():
    """This is in __all__ so we cannot prove it's unused."""
    pass


# Case 3: Decorated function – could be registered with a framework
def my_decorator(fn):
    return fn


@my_decorator
def decorated_target():
    """Carries a decorator; classification must be REVIEW."""
    pass


# Case 4: A normal function that truly is unused and has no uncertainty
def really_unused():
    """No references, no decorator, not in __all__ → PROVABLE."""
    pass
