# alpha.py and beta.py both define 'shared_func'.
# caller.py calls shared_func() with no import (same-package call).
# Because two local definitions share the bare name, both get the reference
# via bare-name fallback and end up uncertain (ACTIVE with ambiguity).
def shared_func():
    return "alpha"
