# caller.py — calls shared_func() with no import.
# bare-name matching means both alpha.shared_func and beta.shared_func
# receive the reference.  Neither can be proven unused.

def run():
    shared_func()  # noqa: F821 — intentionally unimported for testing
