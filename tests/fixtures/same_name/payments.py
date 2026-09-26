# payments.py — also defines calculate_total (same name, different module)
def calculate_total(payments):
    return sum(p["amount"] for p in payments)
