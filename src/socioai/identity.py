import re


def normalize_phone(value: str) -> str:
    """Return E.164-like digits. Brazilian local numbers receive country code 55."""
    value = value.split("@")[0].split(":")[0]
    digits = re.sub(r"\D", "", value)
    if len(digits) in (10, 11):
        digits = "55" + digits
    if not 10 <= len(digits) <= 15:
        raise ValueError("invalid phone number")
    return "+" + digits

