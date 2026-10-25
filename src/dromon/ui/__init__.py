"""The interactive clients of ``dromon attach``, as a core and two platforms.

The layout follows the Rust family's. The core is the counterpart of a ``no_std``
crate. It performs no input or output, imports no terminal, window or socket library,
and is a pure state machine in the Elm style. ``update`` takes the model and an event
and returns the next model and the effects to perform, and ``frame`` turns a model into
rows of styled text with a cursor. Its modules are ``keys``, ``editor``,
``commands``, ``data``, ``state`` and ``render``.

``runtime`` is the shared driver. It owns the model, performs effects through a
``Daemon`` port and feeds the results back as events. The platforms implement the
remaining ports. ``terminal`` draws frames with curses and translates its keys, and
``window`` does the same in a Tk window. The entry point in ``dromon.cli`` wires a
platform, a runtime and the daemon's socket together.

A test enforces the boundary, by importing each core module and checking that no
platform library came with it.

Porting note. The core maps onto a Rust crate with an ``enum`` of events and effects
and a total ``update`` function, and from there onto a Keleusma stream processor. The
platforms map onto Ratatui with Crossterm and onto a native window toolkit.
"""
