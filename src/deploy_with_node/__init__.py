"""Public API for deploy-with-node.

Only `iterate_filesystem` and `EntryRecord` are exported. Importing the package
should never pull in heavy machinery, so the core module is imported lazily at
the top level — it is pure-stdlib and cheap, but keeping the surface explicit
makes it obvious to readers what they are getting.
"""

from .core import EntryRecord, iterate_filesystem

__all__ = ["EntryRecord", "iterate_filesystem"]
