"""Agent backend drivers.

A driver owns exactly three things: build the launch invocation, interpret the exit,
report usage. Everything else — worktree lifecycle, tmux window naming, the sentinel
protocol, brief construction, state and journal — stays in the modules around it.

Core code branches on **capabilities, never on a backend name**. The only module allowed
to know a driver by name is `maestro.backends.registry`, whose whole job is name lookup.

Nothing in this package is imported eagerly: `registry` resolves driver modules lazily so
that importing the package never touches a driver, a binary or the network.
"""
