import string
import secrets

from enochecker3 import (
    MumbleException,
    OfflineException,
)

from phone import TelepwnError

NONZERO_DIGITS = "".join([c for c in string.digits if c != "0"])
def random_phone_safe_number(length): # no repeating digits, not starting with zero
    prev_digit = 0
    num = ""
    for _ in range(length):
        next_digit = secrets.randbelow(9) # 0..8
        if next_digit >= prev_digit:
            next_digit += 1
        num += str(next_digit)
        prev_digit = next_digit
    return num
    #candidate = secrets.choice(NONZERO_DIGITS) + "".join([secrets.choice(string.digits) for i in range(length - 1)])

def ITEM_ID():
    return random_phone_safe_number(13)
def PIN():
    return random_phone_safe_number(10)

FLAG_ALPHABET = "".join(string.ascii_letters + string.digits + "{}+/=-_")
def random_noise():
    length = secrets.randbelow(7) + 20 # XXX: this kinda bad? should be flag length maybe?
    text = "".join(secrets.choice(FLAG_ALPHABET) for _ in range(length))
    return text

class CheckerExceptionTranslator:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if isinstance(exc_val, ConnectionRefusedError):
            raise OfflineException("failed to connect to service")

        if isinstance(exc_val, TelepwnError):
            raise MumbleException(str(exc_val))

        if isinstance(exc_val, BrokenPipeError):
            raise MumbleException(str(exc_val))

async def db_set(db, key, *vals):
    _vals = []
    for val in vals:
        _vals.append((str(val), isinstance(val, int)))
    await db.set(key, _vals)


async def db_get(db, key):
    _vals = []
    for val in await db.get(key):
        _vals.append(int(val[0]) if val[1] else val[0])
    return _vals
