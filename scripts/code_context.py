"""Read-only code context adapters. Provider output is untrusted evidence.

Graphify JSON works offline. GitNexus and Serena use a host-owned MCP peer;
no executable, scope, repository or provider can be selected by recalled text.
"""
import json

from .knowledge_layer import checked, relative_path, canonical
from .memory_policy import check_text, bounded_integer
from .memory_packets import token_counter


class GraphifyFile:
    def __init__(self, sources, graph_path, manifest):
        self.sources = sources
        self.graph_path = relative_path(graph_path)
        # Manifest must be captured by the host at graph construction time.
        self.manifest = checked(manifest)
        if not isinstance(manifest, dict) or len(manifest) > 2000:
            raise ValueError("invalid index manifest")
        from .knowledge_layer import Knowledge
        for path, sha in manifest.items():
            Knowledge._binding({"path": path, "sha256": sha})

    def context(self, symbol):
        check_text(symbol, maximum=256)
        graph = json.loads(self.sources.read(self.graph_path))
        nodes, links = graph.get("nodes"), graph.get("links", graph.get("edges"))
        if not isinstance(nodes, list) or not isinstance(links, list) or len(nodes) > 10000 or len(links) > 50000:
            raise ValueError("unsupported or oversized Graphify graph")
        ids = [n["id"] for n in nodes]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate graph nodes")
        selected = [n for n in nodes if n.get("label") == symbol or n["id"] == symbol]
        if len(selected) != 1:
            return {"status": "not_found" if not selected else "ambiguous", "matches": len(selected)}
        centre = selected[0]["id"]
        related = [e for e in links if centre in (e["source"], e["target"])]
        neighbours = {centre} | {e[k] for e in related for k in ("source", "target")}
        if not neighbours.issubset(set(ids)):
            raise ValueError("unresolved graph endpoint")
        evidence = [n for n in nodes if n["id"] in neighbours]
        paths = {n.get("source_file") for n in evidence} | {e.get("source_file") for e in related}
        statuses = []
        for path in sorted(p for p in paths if p is not None):
            relative_path(path)
            state = self.sources.inspect({"path": path, "sha256": self.manifest[path]}) if path in self.manifest else "unverified"
            statuses.append({"path": path, "state": state})
        # A graph file may be fresh while its source index is stale.
        state = "fresh" if paths and None not in paths and all(s["state"] == "fresh" for s in statuses) else "unverified"
        if any(s["state"] not in {"fresh", "unverified"} for s in statuses):
            return {"status": "stale_index", "sources": statuses, "nodes": [], "edges": []}
        return {"status": state, "sources": statuses, "nodes": evidence, "edges": related,
                "complete_dependency_analysis": False}


class MCPCodeProvider:
    def __init__(self, name, peer, *, repository=None):
        if name not in {"gitnexus", "serena"}:
            raise ValueError("unsupported code provider")
        if name == "gitnexus":
            check_text(repository, maximum=256)
            if not repository or repository.startswith("@"):
                raise ValueError("one explicit repository is required")
        self.name, self.peer, self.repository = name, peer, repository

    def context(self, symbol):
        check_text(symbol, maximum=256)
        if self.name == "gitnexus":
            tool, args = "context", {"name": symbol, "repo": self.repository}
        else:
            tool, args = "find_symbol", {"name_path_pattern": symbol, "include_body": False}
        result = self.peer.call_tool(tool, args)
        if result.get("isError"):
            raise ValueError("code provider rejected the request")
        return {"status": "provider_reported_unverified", "tool": tool, "result": checked(result),
                "complete_dependency_analysis": False}


def context_packet(provider, symbol, *, max_tokens=700, count=None):
    bounded_integer(max_tokens, "max_tokens", minimum=64, maximum=8000)
    count = count or token_counter()
    result = checked({"data_only": True, "context": provider.context(symbol)})
    if count(canonical(result)) > max_tokens:
        # Never split an edge, source location or uncertainty statement.
        result = {"data_only": True, "omitted": True, "reason": "code_context_exceeds_budget"}
        if count(canonical(result)) > max_tokens:
            raise ValueError("budget too small for code-context omission receipt")
    return result
