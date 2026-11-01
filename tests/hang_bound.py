"""One generous wall-clock bound for test waits that exist only to stop a hang.

Such a wait never asserts speed: a correct child process answers long before
the bound, however loaded the host is. Short literals (5-20 s) made correct
code fail under the parallel full suite (-n 8 on a four-core host), where a
fresh interpreter plus a few git commands can take tens of seconds. A wait
that *tests* an enforced deadline must not use this bound; it injects its own
clock or timeout instead.
"""

HANG_SECONDS = 300
