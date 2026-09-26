# orders.py — imports calculate_total explicitly from utils, not payments
from utils import calculate_total


def process_order(items):
    return calculate_total(items)
