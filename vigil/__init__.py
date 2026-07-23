__version__ = "0.1.0"

# No top-level convenience import: Vigil runs as two separate processes
# (see vigil.collector.main.VigilEngine, vigil.web.engine.VigilWebEngine),
# and importing either here would always pull in one process's dependencies
# (real SSH machinery, or NiceGUI) even for callers that only need the other.