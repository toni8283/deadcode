# test_module.py — calls tested_function (test file)
from module import tested_function


def test_it():
    assert tested_function() == 99
