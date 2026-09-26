# module.py — uses importlib.import_module (dynamic import)
import importlib


def load_plugin(name: str):
    """Uses dynamic import — must be REVIEW (contains_dynamic_call)."""
    mod = importlib.import_module(name)
    return mod


def plain_unused():
    """No dynamic calls, no references — PROVABLE."""
    pass
