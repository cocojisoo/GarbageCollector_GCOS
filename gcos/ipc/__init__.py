"""GCOS IPC — message bus, pipes, {INPUT} substitution."""

from gcos.ipc.message_bus import MessageBus, resolve_input_placeholder

__all__ = ["MessageBus", "resolve_input_placeholder"]
