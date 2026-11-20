"""Conductor adapter selection; engine-specific imports stay on the client."""


def local_runtime(config, *, daemon):
    if config.runtime_adapter == "headless":
        from .headless.runtime import HeadlessRuntime

        return HeadlessRuntime(config)
    from .conductor.native import Conductor
    from .conductor.session_runtime import ConductorRuntime

    return ConductorRuntime(config, Conductor(config, client=not daemon))
