"""Normalize TopoDS casts for the browser build of OCP."""

from OCP.TopoDS import TopoDS

for name in ("Edge", "Face", "Shell", "Solid", "Wire"):
    alias = f"{name}_s"
    if not hasattr(TopoDS, alias):
        setattr(TopoDS, alias, getattr(TopoDS, name))
