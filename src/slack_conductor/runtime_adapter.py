"""Conductor adapter selection; engine-specific imports stay on the client."""


def local_runtime(config):
    from .conductor.native import Conductor
    from .conductor.session_runtime import ConductorRuntime

    return ConductorRuntime(config, Conductor(config, client=True))
