"""The private NiceGUI surface Vigil touches, gathered in one module.

None of these attributes are public API, so a NiceGUI upgrade is allowed to
break exactly this file and nowhere else. Each wrapper names the behavior the
caller actually wants; verified against NiceGUI 3.12.
"""

from typing import Any, List

from nicegui import Client, binding
from nicegui import helpers as _helpers

# Dataclass whose fields are NiceGUI bindable properties (PluginModel).
bindable_dataclass = binding.bindable_dataclass


def should_await(result: Any) -> bool:
    """NiceGUI's own test for a callback that returned an awaitable."""
    return _helpers.should_await(result)


def client_is_live(client: Any) -> bool:
    """Whether NiceGUI still tracks this client's connection."""
    return client is not None and client.id in Client.instances


def element_is_attached(client: Any, element: Any) -> bool:
    """Whether an element still exists in the client's element tree."""
    return element is not None and element.id in client.elements


def tree_nodes(tree: Any) -> List[dict]:
    """The node list behind a ui.tree; there is no public reader."""
    return tree._props['nodes']


def set_tree_nodes(tree: Any, nodes: List[dict]) -> None:
    """Replace a ui.tree's nodes; the caller must still call tree.update()."""
    tree._props['nodes'] = nodes


def set_tree_expanded(tree: Any, node_ids: list) -> None:
    """Set a ui.tree's expanded set; the caller must still call tree.update()."""
    tree._props['expanded'] = node_ids
