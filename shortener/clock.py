"""Clock abstraction.

A clock is just a function returning the current time as Unix epoch seconds.
Everything that needs "now" takes a clock, so tests can pass a fake one and
move time forward without sleeping.
"""
import time
from typing import Callable

Clock = Callable[[], float]

system_clock: Clock = time.time
