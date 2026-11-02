"""Direct in-process AetherMind adapter.

This is the vendored mcp/aethermind_mcp.py adapter with the FastMCP server and
decorator setup removed. It exposes the same adapter-v1 tool names, schemas and
semantics to the existing AetherMindClient through list_tools()/call_tool().
No socket, server, or HTTP transport is involved.
"""

import json
from typing import List, Optional

from .vendor_aethermind.aethermind.core import (
    AetherMind,
    error_payload as _error_payload,
    initialize_store as _initialize_store,
    register_project_key as _register_project_key,
    runtime_capabilities as _runtime_capabilities,
)
from .vendor_aethermind.aethermind.inheritance import (
    prepare_inheritance as _prepare_inheritance,
    prepare_discovery as _prepare_discovery,
    verify_use as _verify_use,
)


def _posture_of(layer, inactive):
    layer_id = getattr(layer, "id", None)
    if layer_id and layer_id in (inactive or {}):
        return "archived"
    return "live"


def _layer_to_dict(layer, inactive=None):
    return {
        "id": getattr(layer, "id", None),
        "posture": _posture_of(layer, inactive or {}),
        "timestamp": getattr(layer, "ts", None),
        "ts": getattr(layer, "ts", None),
        "author": getattr(layer, "author", None),
        "type": getattr(layer, "type", None),
        "body": getattr(layer, "body", None),
        "ctx": getattr(layer, "ctx", None),
        "conf": getattr(layer, "conf", None),
        "markers": list(getattr(layer, "markers", []) or []),
        "primitive": getattr(layer, "primitive", "layer"),
        "thread_key": getattr(layer, "thread_key", None),
        "supersedes": list(getattr(layer, "supersedes", []) or []),
        "rollback_of": getattr(layer, "rollback_of", []),
        "corrects": list(getattr(layer, "corrects", []) or []),
        "prev_hash": getattr(layer, "prev_hash", None),
        "receipt_hash": getattr(layer, "receipt_hash", None),
        "evidence": getattr(layer, "evidence", []),
        "recurrence_of": getattr(layer, "recurrence_of", []),
        "verification": getattr(layer, "verification", []),
        "artifact": getattr(layer, "artifact", None),
        "artifact_ref": getattr(layer, "artifact_ref", None),
        "anchor": getattr(layer, "anchor", None),
        "ref": getattr(layer, "ref", None),
        "kind": getattr(layer, "kind", None),
        "label": getattr(layer, "label", None),
        "host": getattr(layer, "host", None),
        "repo_root": getattr(layer, "repo_root", None),
        "content_id": getattr(layer, "content_id", None),
        "selector": getattr(layer, "selector", None),
        "span_hint": getattr(layer, "span_hint", None),
        "domain": getattr(layer, "domain", None),
        "symptom": getattr(layer, "symptom", None),
        "next_verification": getattr(layer, "next_verification", None),
        "suspected_mechanism": getattr(layer, "suspected_mechanism", None),
        "scope": getattr(layer, "scope", None),
        "severity": getattr(layer, "severity", None),
        "owner_hint": getattr(layer, "owner_hint", None),
        "repair": getattr(layer, "repair", None),
        "reason": getattr(layer, "reason", None),
        "replacement": getattr(layer, "replacement", None),
        "restored_to": getattr(layer, "restored_to", None),
        "inline": getattr(layer, "inline", None),
    }


def _string():
    return {"type": "string"}


def _integer():
    return {"type": "integer"}


def _number():
    return {"type": "number"}


def _string_array():
    return {"type": "array", "items": {"type": "string"}}


TOOL_SCHEMAS = {
    "prepare_inheritance": {
        "type": "object",
        "properties": {
            "project_root": _string(),
            "anchor": {"anyOf": [_string(), {"type": "null"}]},
            "budget": _integer(),
            "surface": {"anyOf": [_string(), {"type": "null"}]},
            "session_id": {"anyOf": [_string(), {"type": "null"}]},
        },
        "required": ["project_root"],
        "additionalProperties": False,
    },
    "discover_inheritance": {
        "type": "object",
        "properties": {
            "project_root": _string(),
            "surface": {"anyOf": [_string(), {"type": "null"}]},
            "session_id": {"anyOf": [_string(), {"type": "null"}]},
        },
        "required": ["project_root"],
        "additionalProperties": False,
    },
    "verify_inheritance_use": {
        "type": "object",
        "properties": {"envelope": {"type": "object"}, "receipt": {"type": "object"}},
        "required": ["envelope", "receipt"],
        "additionalProperties": False,
    },
    "write_layer": {
        "type": "object",
        "properties": {
            "project_root": _string(),
            "layer_type": _string(),
            "body": _string(),
            "ctx": _string(),
            "conf": _number(),
            "markers": _string_array(),
            "author": {"anyOf": [_string(), {"type": "null"}]},
            "primitive": _string(),
            "thread_key": {"anyOf": [_string(), {"type": "null"}]},
            "supersedes": {"anyOf": [_string_array(), {"type": "null"}]},
            "rollback_of": {"anyOf": [_string_array(), {"type": "null"}]},
            "corrects": {"anyOf": [_string_array(), {"type": "null"}]},
            "evidence": {"anyOf": [_string_array(), {"type": "null"}]},
            "recurrence_of": {"anyOf": [_string_array(), {"type": "null"}]},
            "verification": {"anyOf": [_string_array(), {"type": "null"}]},
            "artifact": {"anyOf": [_string(), {"type": "null"}]},
            "artifact_ref": {"anyOf": [_string(), {"type": "null"}]},
            "anchor": {"anyOf": [_string(), {"type": "null"}]},
            "ref": {"anyOf": [_string(), {"type": "null"}]},
            "kind": {"anyOf": [_string(), {"type": "null"}]},
            "label": {"anyOf": [_string(), {"type": "null"}]},
            "host": {"anyOf": [_string(), {"type": "null"}]},
            "repo_root": {"anyOf": [_string(), {"type": "null"}]},
            "content_id": {"anyOf": [_string(), {"type": "null"}]},
            "selector": {"anyOf": [_string(), {"type": "null"}]},
            "span_hint": {"anyOf": [_string(), {"type": "null"}]},
            "domain": {"anyOf": [_string(), {"type": "null"}]},
            "symptom": {"anyOf": [_string(), {"type": "null"}]},
            "next_verification": {"anyOf": [_string(), {"type": "null"}]},
            "suspected_mechanism": {"anyOf": [_string(), {"type": "null"}]},
            "scope": {"anyOf": [_string(), {"type": "null"}]},
            "severity": {"anyOf": [_string(), {"type": "null"}]},
            "owner_hint": {"anyOf": [_string(), {"type": "null"}]},
            "repair": {"anyOf": [_string(), {"type": "null"}]},
            "reason": {"anyOf": [_string(), {"type": "null"}]},
            "replacement": {"anyOf": [_string(), {"type": "null"}]},
            "restored_to": {"anyOf": [_string(), {"type": "null"}]},
            "inline": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
            "create": {"type": "boolean"},
        },
        "required": ["project_root", "layer_type", "body", "ctx", "conf", "markers"],
        "additionalProperties": False,
    },
    "write_texture": {
        "type": "object",
        "properties": {
            "project_root": _string(),
            "body": _string(),
            "ctx": _string(),
            "marker": {"anyOf": [_string(), {"type": "null"}]},
            "author": {"anyOf": [_string(), {"type": "null"}]},
            "create": {"type": "boolean"},
        },
        "required": ["project_root", "body", "ctx"],
        "additionalProperties": False,
    },
    "read_layers": {
        "type": "object",
        "properties": {
            "project_root": _string(),
            "ctx": {"anyOf": [_string(), {"type": "null"}]},
            "type": {"anyOf": [_string(), {"type": "null"}]},
            "author": {"anyOf": [_string(), {"type": "null"}]},
            "since_ts": {"anyOf": [_string(), {"type": "null"}]},
            "last_n": {"anyOf": [_integer(), {"type": "null"}]},
            "ctx_prefix": {"anyOf": [_string(), {"type": "null"}]},
            "markers_any": {"anyOf": [_string_array(), {"type": "null"}]},
            "posture": {"anyOf": [_string(), {"type": "null"}]},
            "text": {"anyOf": [_string(), {"type": "null"}]},
        },
        "required": ["project_root"],
        "additionalProperties": False,
    },
    "read_texture": {
        "type": "object",
        "properties": {"project_root": _string()},
        "required": ["project_root"],
        "additionalProperties": False,
    },
    "status": {
        "type": "object",
        "properties": {"project_root": _string()},
        "required": ["project_root"],
        "additionalProperties": False,
    },
    "brief": {
        "type": "object",
        "properties": {
            "project_root": _string(),
            "as_of": {"anyOf": [_string(), {"type": "null"}]},
            "anchor": {"anyOf": [_string(), {"type": "null"}]},
            "budget": {"anyOf": [_integer(), {"type": "null"}]},
        },
        "required": ["project_root"],
        "additionalProperties": False,
    },
    "audit": {
        "type": "object",
        "properties": {"project_root": _string()},
        "required": ["project_root"],
        "additionalProperties": False,
    },
    "gate_check": {
        "type": "object",
        "properties": {
            "project_root": _string(),
            "anchor": {"anyOf": [_string(), {"type": "null"}]},
            "planned_type": _string(),
            "planned_ctx": {"anyOf": [_string(), {"type": "null"}]},
            "enforce_gate": {"type": "boolean"},
        },
        "required": ["project_root"],
        "additionalProperties": False,
    },
    "write_event": {
        "type": "object",
        "properties": {
            "project_root": _string(),
            "event_type": _string(),
            "body": _string(),
            "ctx": _string(),
            "author": {"anyOf": [_string(), {"type": "null"}]},
        },
        "required": ["project_root", "event_type", "body", "ctx"],
        "additionalProperties": False,
    },
    "read_events": {
        "type": "object",
        "properties": {"project_root": _string()},
        "required": ["project_root"],
        "additionalProperties": False,
    },
    "archive": {
        "type": "object",
        "properties": {
            "project_root": _string(),
            "layer_ids": _string_array(),
            "reason": {"anyOf": [_string(), {"type": "null"}]},
        },
        "required": ["project_root", "layer_ids"],
        "additionalProperties": False,
    },
    "currentness": {
        "type": "object",
        "properties": {
            "project_root": _string(),
            "as_of": {"anyOf": [_string(), {"type": "null"}]},
        },
        "required": ["project_root"],
        "additionalProperties": False,
    },
    "initialize_store": {
        "type": "object",
        "properties": {
            "project_root": _string(),
            "project_key": {"anyOf": [_string(), {"type": "null"}]},
        },
        "required": ["project_root"],
        "additionalProperties": False,
    },
    "capabilities": {
        "type": "object",
        "properties": {"project_root": {"anyOf": [_string(), {"type": "null"}]}},
        "required": [],
        "additionalProperties": False,
    },
    "register_project_key": {
        "type": "object",
        "properties": {
            "project_root": _string(),
            "key": _string(),
            "overwrite": {"type": "boolean"},
        },
        "required": ["project_root", "key"],
        "additionalProperties": False,
    },
    "view": {
        "type": "object",
        "properties": {
            "project_root": _string(),
            "budget_chars": _integer(),
            "recent_window": _integer(),
        },
        "required": ["project_root"],
        "additionalProperties": False,
    },
    "zoom": {
        "type": "object",
        "properties": {
            "project_root": _string(),
            "level": _integer(),
            "index": _integer(),
        },
        "required": ["project_root", "level", "index"],
        "additionalProperties": False,
    },
}

TOOL_DESCRIPTIONS = {
    "prepare_inheritance": "Read a native AetherMind inheritance envelope on this filesystem host.",
    "discover_inheritance": "Read available AetherMind context/anchor and must-know references.",
    "verify_inheritance_use": "Validate a use-evidence claim's shape and packet/session binding only.",
    "write_layer": "Write a layer to the AetherMind project.",
    "write_texture": "Write a texture entry to the AetherMind project.",
    "read_layers": "Return filtered layers as JSON.",
    "read_texture": "Return the full texture file content.",
    "status": "Return layer count, last author, last timestamp, and type breakdown.",
    "brief": "Return active digests and per-scope deltas at an optional timestamp.",
    "audit": "Scan the ledger hash chain and report integrity (audit).",
    "gate_check": "Check a proposed mutation against must-know corrections (gate).",
    "write_event": "Append a cheap routine event to events.aem (split ledger).",
    "read_events": "Read events from events.aem (split ledger); empty store is healthy.",
    "archive": "Archive layers to archive.aem (tombstone + continuation header).",
    "currentness": "Return immutable history, active heads, and explicit inactive reasons.",
    "initialize_store": "Explicitly create store metadata without writing a layer or texture.",
    "capabilities": "Return the canonical runtime identity and capability contract.",
    "register_project_key": "Declare a stable project key for the store at project_root.",
    "view": "Render the hybrid brief: whole-store coarse cover + full-res must-knows.",
    "zoom": "Zoom into one summary-tree node: its line plus its children's lines.",
}


class DirectMcpClient:
    """The local callable client the existing AetherMindClient probes and calls.

    It intentionally mirrors McpClient's surface: timeout, list_tools(),
    call_tool(). No I/O leaves this process.
    """

    def __init__(self, timeout=15):
        self.timeout = timeout

    def list_tools(self):
        tools = []
        for name, schema in TOOL_SCHEMAS.items():
            tools.append({
                "name": name,
                "description": TOOL_DESCRIPTIONS[name],
                "inputSchema": json.loads(json.dumps(schema)),
            })
        return tools

    def call_tool(self, name, arguments=None):
        arguments = dict(arguments or {})
        handler = getattr(self, "_tool_" + name, None)
        if handler is None:
            return _error_payload(ValueError("Unknown AetherMind tool: " + str(name)))
        try:
            return handler(**arguments)
        except Exception as error:
            return _error_payload(error)


def _json_error(error):
    return _error_payload(error)


def _project(arguments):
    root = arguments.get("project_root")
    if not isinstance(root, str) or not root.strip():
        raise ValueError("project_root is required")
    return root


class _DirectTools(DirectMcpClient):
    pass


def _attach_handlers(cls):
    def prepare_inheritance(self, project_root, anchor=None, budget=3000, surface=None, session_id=None):
        try:
            result = _prepare_inheritance(project_root, anchor, budget, surface, session_id)
            return {"success": True, "envelope": result}
        except Exception as error:
            return _json_error(error)

    def discover_inheritance(self, project_root, surface=None, session_id=None):
        try:
            result = _prepare_discovery(project_root, surface, session_id)
            return {"success": True, "manifest": result}
        except Exception as error:
            return _json_error(error)

    def verify_inheritance_use(self, envelope, receipt):
        try:
            return {"success": True, **_verify_use(envelope, receipt), "authority_granted": False}
        except Exception as error:
            return _json_error(error)

    def write_layer(self, project_root, layer_type, body, ctx, conf, markers, author=None,
                    primitive="layer", thread_key=None, supersedes=None, rollback_of=None,
                    corrects=None, evidence=None, recurrence_of=None, verification=None,
                    artifact=None, artifact_ref=None, anchor=None, ref=None, kind=None,
                    label=None, host=None, repo_root=None, content_id=None, selector=None,
                    span_hint=None, domain=None, symptom=None, next_verification=None,
                    suspected_mechanism=None, scope=None, severity=None, owner_hint=None,
                    repair=None, reason=None, replacement=None, restored_to=None, inline=None,
                    create=False):
        try:
            aethermind = AetherMind(project_root, create=create)
            layer = aethermind.write_layer(
                layer_type, body, ctx, conf, markers, author=author, primitive=primitive,
                thread_key=thread_key, supersedes=supersedes, rollback_of=rollback_of,
                corrects=corrects, evidence=evidence, recurrence_of=recurrence_of,
                verification=verification, artifact=artifact, artifact_ref=artifact_ref,
                anchor=anchor, ref=ref, kind=kind, label=label, host=host,
                repo_root=repo_root, content_id=content_id, selector=selector,
                span_hint=span_hint, domain=domain, symptom=symptom,
                next_verification=next_verification, suspected_mechanism=suspected_mechanism,
                scope=scope, severity=severity, owner_hint=owner_hint, repair=repair,
                reason=reason, replacement=replacement, restored_to=restored_to, inline=inline)
            return {
                "success": True,
                "layer_id": getattr(layer, "id", None),
                "hash": getattr(layer, "receipt_hash", None),
                "timestamp": getattr(layer, "ts", None),
                "ts": getattr(layer, "ts", None),
                "author": getattr(layer, "author", None),
                "layer": _layer_to_dict(layer),
                "message": "Layer written successfully",
            }
        except Exception as error:
            return _json_error(error)

    def write_texture(self, project_root, body, ctx, marker=None, author=None, create=False):
        try:
            aethermind = AetherMind(project_root, create=create)
            entry = aethermind.write_texture(body, ctx, marker, author=author)
            return {"success": True, "entry": entry, "message": "Texture entry written successfully"}
        except Exception as error:
            return _json_error(error)

    def read_layers(self, project_root, ctx=None, type=None, author=None, since_ts=None,
                    last_n=None, ctx_prefix=None, markers_any=None, posture=None, text=None):
        try:
            if posture not in (None, "live", "archived", "all"):
                raise ValueError("posture must be live, archived, or all")
            aethermind = AetherMind(project_root, create=False)
            aethermind._require_store()
            layers = aethermind.read_layers(ctx=ctx, type=type, author=author,
                                            since_ts=since_ts, last_n=None,
                                            ctx_prefix=ctx_prefix, markers_any=markers_any,
                                            text=text)
            inactive = aethermind.currentness().get("inactive") or {}
            selected = []
            for layer in layers:
                layer_posture = _posture_of(layer, inactive)
                if posture in ("live", "archived") and layer_posture != posture:
                    continue
                selected.append(layer)
            if last_n is not None:
                selected = sorted(
                    selected,
                    key=lambda layer: (getattr(layer, "ts", "") or "", getattr(layer, "id", "") or ""),
                    reverse=True,
                )[: max(0, int(last_n))]
            return {
                "success": True,
                "store": str(aethermind.project_root),
                "layers": [_layer_to_dict(layer, inactive) for layer in selected],
                "count": len(selected),
            }
        except Exception as error:
            return _json_error(error)

    def read_texture(self, project_root):
        try:
            aethermind = AetherMind(project_root, create=False)
            texture = aethermind.read_texture()
            return {"success": True, "texture": texture, "count": len(texture)}
        except Exception as error:
            return _json_error(error)

    def status(self, project_root):
        try:
            aethermind = AetherMind(project_root, create=False)
            summary = aethermind.status_summary()
            return {"success": True, **summary}
        except Exception as error:
            return _json_error(error)

    def brief(self, project_root, as_of=None, anchor=None, budget=None):
        try:
            aethermind = AetherMind(project_root, create=False)
            if anchor is not None or budget is not None:
                result = aethermind.brief_anchor(anchor=anchor, budget=budget if budget is not None else 4000,
                                                as_of=as_of)
                return {
                    "success": True,
                    "anchor": result["anchor"],
                    "must_know": result["must_know"],
                    "unabsorbed_debt": result["unabsorbed_debt"],
                    "kept": [_layer_to_dict(layer) for layer in result["kept"]],
                    "tombstones": result["tombstones"],
                    "budget": result["budget"],
                    "budget_used": result["budget_used"],
                    "layer_count": result["layer_count"],
                    "determinism_key": result["determinism_key"],
                }
            result = aethermind.brief(as_of=as_of)
            return {
                "success": True,
                "digests": [_layer_to_dict(layer) for layer in result["digests"]],
                "deltas": [_layer_to_dict(layer) for layer in result["deltas"]],
                "newest_digest_ts": result["newest_digest_ts"],
                "layer_count": result["layer_count"],
                "type_breakdown": result["type_breakdown"],
            }
        except Exception as error:
            return _json_error(error)

    def audit(self, project_root):
        try:
            aethermind = AetherMind(project_root, create=False)
            return {"success": True, **aethermind.audit()}
        except Exception as error:
            return _json_error(error)

    def gate_check(self, project_root, anchor=None, planned_type="layer", planned_ctx=None,
                   enforce_gate=False):
        try:
            aethermind = AetherMind(project_root, create=False)
            plan = {"anchor": anchor, "planned_type": planned_type,
                    "planned_ctx": planned_ctx, "enforce_gate": enforce_gate}
            return {"success": True, **aethermind.gate_check(plan, anchor=anchor)}
        except Exception as error:
            return _json_error(error)

    def write_event(self, project_root, event_type, body, ctx, author=None):
        try:
            aethermind = AetherMind(project_root, create=False)
            return {"success": True, **aethermind.write_event(type=event_type, body=body, ctx=ctx, author=author)}
        except Exception as error:
            return _json_error(error)

    def read_events(self, project_root):
        try:
            aethermind = AetherMind(project_root, create=False)
            return {"success": True, **aethermind.read_events()}
        except Exception as error:
            return _json_error(error)

    def archive(self, project_root, layer_ids, reason=None):
        try:
            aethermind = AetherMind(project_root, create=False)
            return {"success": True, **aethermind.archive(layer_ids, reason=reason or "")}
        except Exception as error:
            return _json_error(error)

    def currentness(self, project_root, as_of=None):
        try:
            aethermind = AetherMind(project_root, create=False)
            result = aethermind.currentness(as_of=as_of)
            return {
                "success": True,
                "history": [_layer_to_dict(layer) for layer in result["history"]],
                "active_heads": [_layer_to_dict(layer) for layer in result["active_heads"]],
                "inactive": result["inactive"],
                "unresolved_currentness": [_layer_to_dict(layer) for layer in result["unresolved_currentness"]],
                "as_of": result["as_of"],
            }
        except Exception as error:
            return _json_error(error)

    def initialize_store(self, project_root, project_key=None):
        try:
            return {"success": True, **_initialize_store(project_root, project_key=project_key)}
        except Exception as error:
            return _json_error(error)

    def capabilities(self, project_root=None):
        try:
            return {"success": True, "runtime": _runtime_capabilities(project_root)}
        except Exception as error:
            return _json_error(error)

    def register_project_key(self, project_root, key, overwrite=False):
        try:
            return {"success": True, **_register_project_key(project_root, key, overwrite=overwrite)}
        except Exception as error:
            return _json_error(error)

    def view(self, project_root, budget_chars=3600, recent_window=64):
        try:
            from .vendor_aethermind.aethermind.view import render_view, view_cost
            text = render_view(project_root, budget_chars=budget_chars, recent_window=recent_window)
            return {"success": True, "view": text, **view_cost(text)}
        except Exception as error:
            return _json_error(error)

    def zoom(self, project_root, level, index):
        try:
            from .vendor_aethermind.aethermind.tree import TreeStore, default_tree_path
            from pathlib import Path
            tree_path = default_tree_path(Path(project_root))
            store = TreeStore(tree_path)
            nodes = store.index()
            node = nodes.get((level, index))
            if node is None:
                return {"success": False, "error": f"no node at (level={level}, index={index})"}
            child_level = level - 1
            left = nodes.get((child_level, index * 2))
            right = nodes.get((child_level, index * 2 + 1))
            return {
                "success": True,
                "level": node.level,
                "index": node.index,
                "start": node.start,
                "end": node.end,
                "cut": node.cut,
                "text": node.text,
                "children": [
                    {"level": child.level, "index": child.index, "text": child.text, "cut": child.cut}
                    for child in (left, right) if child is not None
                ],
            }
        except Exception as error:
            return _json_error(error)

    for name, function in list(locals().items()):
        if name in TOOL_SCHEMAS and callable(function):
            setattr(cls, "_tool_" + name, function)
    return cls


_attach_handlers(DirectMcpClient)
