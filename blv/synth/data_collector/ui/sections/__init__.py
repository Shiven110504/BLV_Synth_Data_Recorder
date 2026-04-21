"""Section modules — each implements a collapsible frame.

Every section follows the same contract::

    class Section:
        def __init__(self, parent_vstack, session, widgets, style) -> None: ...
        def on_tick(self) -> None: ...   # refresh labels from session state
        def destroy(self) -> None: ...   # optional cleanup
"""
