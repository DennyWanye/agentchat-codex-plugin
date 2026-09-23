"""Local AgentChat bridge for a Codex task.

The bridge deliberately keeps the remote protocol client separate from the
local handler.  A message received from the network is data; it is never
used as a shell command.
"""

from .client import AgentChatClient, Credentials
from .config import BridgeConfig
from .store import InboxStore

__all__ = ["AgentChatClient", "BridgeConfig", "Credentials", "InboxStore"]
