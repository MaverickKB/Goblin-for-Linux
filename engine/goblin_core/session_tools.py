"""Tools every live Goblin session registers beyond the base set.

Each session gets the computer-use tools selected by its settings and, when
Autonomy is present, the autonomy tools.
"""

from .cua import register_cua_tools
from .host_cua import register_host_cua
from .tools import object_schema


def register_session_tools(host, session, registry):
    """Register host-owned tools on one session registry."""
    register_host_cua(registry, host.config)
    register_cua_tools(registry, host.config)
    if getattr(host, "autonomy", None) is not None:
        runtime = host.session_runtime(session)
        host.autonomy.register_tools(runtime, registry)
    # Optional response-only swarm stays off unless a host adapter provides it.
    if getattr(host, "swarm_enabled", False) and hasattr(host, "create_swarm_job"):
        def swarm_job(action, objective="", workers=None, job_id=""):
            if action == "create":
                return host.create_swarm_job(session.slug if hasattr(session, "slug") else host.slug,
                                            objective, workers)
            if action == "run":
                return host.run_swarm_job(job_id)
            if action == "status":
                return host.swarm_job(job_id)
            if action == "artifact":
                return host.swarm_artifact(job_id)
            raise ValueError("Unknown swarm action")
        registry.register(
            "swarm_job",
            "Create, run or inspect a bounded parallel response-only evidence job. "
            "Workers need objective and non-overlapping scope; backend is optional, "
            "capability selects the cheapest capable configured model. These jobs do not "
            "execute tools or edit files; use spawn_agent for implementation.",
            object_schema({"action": {"type": "string", "enum": ["create", "run", "status", "artifact"]},
                           "objective": {"type": "string"}, "workers": {"type": "array", "items": {"type": "object"}},
                           "job_id": {"type": "string"}}, ["action"]),
            swarm_job,
        )
