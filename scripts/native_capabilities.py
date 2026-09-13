"""Conservative adapter contract from inspected native APIs, not activation.

Presence of an API field does not establish a desktop UI or atomicity promise.
"""
from dataclasses import asdict
from .session_routing import Capabilities


def manifest():
    return {
        "format": "native-routing-capabilities/1",
        "inspected_version": "codex-cli 0.153.4",
        "sources": ["https://developers.openai.com/codex/app-server/",
                    "https://developers.openai.com/codex/hooks/"],
        "observed": {"thread_start": True, "thread_resume_schema": True, "persistent_thread_resume": True,
                     "project_id_parameter": True, "client_user_message_id_parameter": True,
                     "cumulative_thread_token_usage": True},
        "resume_limitation": "thread/resume requires a stored rollout; the ephemeral probe returned no rollout found",
        "dispatch_capabilities": asdict(Capabilities()),
        "automatic_dispatch": False,
        "unverified_contracts": ["atomic interception and transfer of the pending desktop message",
            "ordinary-chat visibility and project placement receipts",
            "atomic source and destination idle/revision guard",
            "global exactly-once delivery across destination creation",
            "authoritative delivery reconciliation"],
        "fallback": "Prepare a durable local handoff. No chat creation or delivery is claimed.",
        "synthetic_only": True,
    }


class HandoffGateway:
    capabilities = Capabilities()

    def observe(self, source):
        raise ValueError("native desktop state is not verified by this handoff adapter")

    def deliver_once(self, request):
        raise ValueError("automatic native desktop dispatch is unavailable")
