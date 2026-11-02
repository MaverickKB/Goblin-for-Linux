"""Settings for one independent Goblin app.

The bridge editor sends exactly the keys in Runtime/BRIDGE.md plus an
``advanced`` object. Every accepted field is consumed by the runtime. Unknown
sections and unknown fields are refused, and secret-looking keys are never
persisted.
"""

from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlsplit

SETTINGS_KEYS = (
    "agent_name", "user_instructions", "workspace", "model_base_url", "model",
    "api_kind", "cli_command", "reasoning", "thinking_budget", "approval",
    "world_view", "floor_interval_seconds", "voice_model", "voice_name",
    "voice_auth", "model_connection", "account_models", "custom_models", "advanced",
)
API_KINDS = ("chat_completions", "responses", "cli_worker")
REASONING_LEVELS = ("off", "low", "medium", "high")
APPROVALS = ("ask", "unleashed")
VOICE_AUTHS = ("oauth", "api_key")
ADVANCED_SECTIONS = ("tools", "skills", "mcp_servers", "remotes", "autonomy", "computer_use", "cua", "compactor", "aethermind")

SECRET_WORDS = ("api_key", "apikey", "token", "secret", "password", "credential")


@dataclass
class ToolsSettings:
    approval: str = "ask"
    approval_classes: dict = field(default_factory=lambda: {
        "memory-self": ["clock_heartbeat", "clock_journal_write", "*write_layer", "skill_write"],
        "acting": ["mac_act", "desk_act", "shell", "write_file", "edit_file", "apply_patch",
                   "move_path", "copy_path", "delete_path", "mkdir", "process",
                   "remote_shell", "remote_write_file", "background_task",
                   "computer_mouse", "computer_scroll", "computer_type", "computer_key",
                   "computer_focus", "computer_click", "computer_perform_secondary_action",
                   "computer_set_value", "computer_select_text", "computer_drag",
                   "computer_press_key", "computer_type_text"],
    })
    approval_rules: dict = field(default_factory=lambda: {"memory-self": "allow", "acting": "gate"})
    disabled: list = field(default_factory=list)
    clip_chars: int = 30000
    timeout: float = 30.0
    list_limit: int = 200
    grep_limit: int = 40
    grep_scan: int = 2000
    git_log_limit: int = 20
    spill_dir: str = "tool-output"
    output_tail_chars: int = 4000
    read_line_limit: int = 400
    glob_limit: int = 200
    read_bytes_limit: int = 1000000
    image_bytes_limit: int = 5000000
    fetch_max_bytes: int = 1000000
    fetch_timeout: float = 20.0
    shell_max_timeout: float = 600.0


@dataclass
class SkillsSettings:
    enabled: bool = True
    shelf: str = ""
    trusted_projects: list = field(default_factory=list)
    project_dirs: list = field(default_factory=lambda: [".agents/skills"])
    note_bytes: int = 4096
    note_description_chars: int = 160
    search_limit: int = 20
    disabled: list = field(default_factory=list)
    extra_dirs: list = field(default_factory=list)


@dataclass
class McpServerConfig:
    name: str = ""
    enabled: bool = True
    description: str = ""
    url: str = ""
    command: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    cwd: str = ""
    token_env: str = ""
    timeout: float = 30.0
    list_ttl: float = 300.0
    include: list = field(default_factory=list)
    exclude: list = field(default_factory=list)
    safe_tools: list = field(default_factory=list)
    tool_prefix: str = ""
    session_tools: bool = True


@dataclass
class RemoteConfig:
    name: str = ""
    enabled: bool = True
    description: str = ""
    host: str = ""
    port: int = 22
    user: str = ""
    identity_file: str = ""
    known_hosts_file: str = ""
    strict_host_key_checking: str = "yes"
    ssh_command: list = field(default_factory=lambda: ["ssh"])
    connect_timeout: int = 10
    timeout: float = 60.0
    rdp_host: str = ""
    rdp_port: int = 0
    rdp_doc: str = ""
    rdp_enabled: bool = True
    desk: bool = False
    desk_screenshot_command: str = "gnome-screenshot -f -"
    desk_input_command: str = "python3 -"


@dataclass
class ViewSettings:
    node_bytes: int = 512
    high_bytes: int = 128000
    low_bytes: int = 64000


@dataclass
class CompactorSettings:
    workers: int = 2
    retries: int = 2
    context_bytes: int = 16000
    # Empty model means inherit the chat backend. Empty model_base_url with a
    # set model inherits the chat endpoint. Override protocols are HTTP only.
    model: str = ""
    model_base_url: str = ""
    api_kind: str = "chat_completions"
    connection: str = ""
    inherit_model: str = ""
    account_models: dict = field(default_factory=dict)


@dataclass
class AgentsSettings:
    enabled: bool = True
    max_depth: int = 2
    view_bytes: int = 16000


@dataclass
class AutonomySettings:
    """Event-driven wakes. Caps gate delivery; held events are never dropped."""
    enabled: bool = False
    state_file: str = "autonomy/state.json"
    tasks_dir: str = "autonomy/tasks"
    floor_file: str = ""
    tick_seconds: float = 1.0
    min_wake_gap_seconds: float = 60.0
    max_wakes_per_hour: int = 0
    max_wakes_per_day: int = 0
    max_timers: int = 24
    min_timer_seconds: float = 30.0
    max_timer_seconds: float = 604800.0
    max_tasks: int = 4
    task_max_seconds: float = 3600.0
    task_stall_seconds: float = 900.0
    keep_finished_tasks: int = 50
    wake_output_chars: int = 1500
    approval_expire_seconds: float = 3600.0
    self_prompt_enabled: bool = True
    self_prompt_gap_seconds: float = 900.0
    standing_floor: bool = True
    floor_slug: str = "goblin"
    floor_name: str = ""
    world_view: str = ""
    lesson_file: str = "learning/lessons.json"
    lesson_pass_score: float = 0.5
    max_open_lessons: int = 12
    lesson_fail_limit: int = 2


@dataclass
class CuaSettings:
    mac_enabled: bool = False
    mac_helper_app: str = "cua/GoblinMacLook.app"
    mac_bundle_id: str = "com.maverickkb.goblin.maclook"
    swiftc: str = "/usr/bin/swiftc"
    codesign_identity: str = "-"
    screencapture: str = "/usr/sbin/screencapture"
    sips: str = "/usr/bin/sips"
    max_image_px: int = 1600
    attach_images: bool = True
    timeout: float = 30.0
    backend: str = "auto"
    screenshot_scale: float = 1.0
    osascript: str = "osascript"
    input_command: str = "xdotool"
    window_command: str = "wmctrl"
    screenshot_commands: list = field(default_factory=lambda: ["scrot", "import", "gnome-screenshot"])


@dataclass
class ComputerUseSettings:
    backend: str = "auto"
    provider: str = ""
    tool_prefix: str = "computer_"
    text_chars: int = 12000
    helper: str = "cua/GoblinCUA.app"
    timeout: float = 30.0


@dataclass
class AetherMindSettings:
    project: str
    mode: str = "embedded"
    local_project: str = ""
    endpoint: str = ""
    token_env: str = ""
    personal_project: str = ""
    note_project: str = ""
    author: str = "goblin"
    auto_recall: bool = True
    recall_limit: int = 10
    recall_scan: int = 400
    timeout: float = 15.0
    create_project: bool = True
    inheritance_budget: int = 4000
    personal_budget: int = 2000
    inheritance_refresh_seconds: float = 900.0
    capture_omit_outcomes: list = field(default_factory=lambda: ["memory_recall", "memory_tree", "session_briefs"])


@dataclass
class VoiceSettings:
    enabled: bool = True
    websocket_url: str = ""
    api_base_url: str = ""
    client_secret_path: str = ""
    client_protocol_prefix: str = ""
    client_protocol_extra: list = field(default_factory=list)
    model: str = ""
    model_query_param: str = "model"
    transcribe_model: str = "grok-transcribe"
    voices: list = field(default_factory=list)
    voice: str = ""
    audio_format: str = "audio/pcm"
    audio_sample_rate: int = 24000
    max_session_seconds: int = 3600
    secret_ttl_seconds: int = 300
    max_response_bytes: int = 1048576
    max_event_bytes: int = 262144
    max_text_bytes: int = 8000
    max_events: int = 4000
    max_sessions: int = 4
    timeout: float = 20.0
    clock_skew_seconds: float = 60.0
    pipe: str = "duplex"
    auth: str = "oauth"
    model_query_selector: str = "model"
    tools_mode: str = "session"
    tools: list = field(default_factory=list)
    dedicated_slug: str = "goblin"
    dedicated_title: str = "Goblin"
    dedicated_reasoning: str = "low"
    dedicated_thinking_budget: int = 0
    turn_detection: dict = field(default_factory=lambda: {"type": "server_vad", "idle_timeout_ms": 800})
    session_options: dict = field(default_factory=lambda: {"audio": {"input": {}, "output": {}}})
    session_shape: str = "xai"
    idle_reap_seconds: float = 0.0
    local_reply_timeout: float = 120.0
    local_max_audio_bytes: int = 8388608
    stt: object = None
    tts: object = None


class ConfigError(ValueError):
    """A settings value the runtime refuses. The message names the field."""


def _path(value):
    if isinstance(value, Path):
        return value
    return Path(str(value)).expanduser().resolve()


def workspace_issue(value, create=False):
    """Return a concrete reason a workspace is unusable, or "" when it is usable.

    A startup check must not raise: the operator still needs a live settings
    surface to repair an invalid saved path. A save check uses create=True so
    the folder is adopted before the settings document is persisted.
    """
    if not isinstance(value, (str, Path)):
        return "Workspace must be a path"
    text = str(value)
    if not text.strip() or "\x00" in text:
        return "Workspace must be a path"
    path = Path(text).expanduser()
    if not path.is_absolute():
        return "Workspace must be an absolute path"
    try:
        if path.exists():
            if not path.is_dir():
                return "Workspace path is a file, not a folder"
        elif create:
            path.mkdir(parents=True, exist_ok=True)
        else:
            return "Workspace folder does not exist"
        if not path.is_dir():
            return "Workspace path is a file, not a folder"
        if not os.access(path, os.W_OK | os.X_OK):
            return "Workspace is not writable"
    except OSError as error:
        return "Workspace is unavailable (" + type(error).__name__ + ")"
    return ""


def _url(value, label, schemes=("http", "https")):
    if value == "":
        return ""
    if not isinstance(value, str) or any(char.isspace() for char in value):
        raise ConfigError(label + " must be empty or a URL")
    parsed = urlsplit(value)
    if parsed.scheme not in schemes or not parsed.hostname or parsed.username is not None:
        raise ConfigError(label + " must be a credential-free " + "/".join(schemes) + " URL")
    for key, _ in parse_qsl(parsed.query):
        if key.lower() in ("token", "api_key", "key", "authorization", "access_token", "client_secret"):
            raise ConfigError(label + " must not carry credentials in its query")
    return value


def _text(value, label, allow_empty=True):
    if not isinstance(value, str):
        raise ConfigError(label + " must be text")
    if not allow_empty and not value.strip():
        raise ConfigError(label + " cannot be empty")
    return value


def _whole(value, label, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(label + " must be an integer")
    if value < minimum:
        raise ConfigError(label + " must be at least " + str(minimum))
    if maximum is not None and value > maximum:
        raise ConfigError(label + " must be at most " + str(maximum))
    return value


def _number(value, label, minimum=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(label + " must be a number")
    if not math.isfinite(value) or value < minimum:
        raise ConfigError(label + " must be a finite number of at least " + str(minimum))
    return float(value)


def _bool(value, label):
    if not isinstance(value, bool):
        raise ConfigError(label + " must be true or false")
    return value


def _string_list(value, label):
    if not isinstance(value, list):
        raise ConfigError(label + " must be a list of strings")
    for item in value:
        if not isinstance(item, str) or not item:
            raise ConfigError(label + " must be a list of nonempty strings")
    return list(value)


def _secret_key(key):
    lowered = str(key).lower()
    if lowered in ("token_env", "api_key_env", "credential_ref"):
        # These name a source; they never hold the secret itself.
        return False
    return any(word in lowered for word in SECRET_WORDS)


def _check_secret_keys(value, label):
    if isinstance(value, dict):
        for key, item in value.items():
            if _secret_key(key):
                raise ConfigError(label + " cannot store secrets")
            _check_secret_keys(item, label)


def _aethermind_settings(raw, home):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("advanced.aethermind must be an object")
    allowed = {"mode", "local_project", "project", "endpoint", "token_env"}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError("advanced.aethermind has unknown fields: " + ", ".join(sorted(unknown)))
    mode = _text(raw.get("mode", "embedded"), "advanced.aethermind.mode")
    if mode not in ("embedded", "remote"):
        raise ConfigError("advanced.aethermind.mode must be embedded or remote")
    local = _text(raw.get("local_project", ""), "advanced.aethermind.local_project")
    local_path = Path(local).expanduser() if local else Path(home) / "aethermind"
    if not local_path.is_absolute():
        local_path = Path(home) / local_path
    local_project = str(local_path.resolve())
    remote_project = _text(raw.get("project", ""), "advanced.aethermind.project")
    endpoint = _url(raw.get("endpoint", ""), "advanced.aethermind.endpoint")
    token_env = _text(raw.get("token_env", ""), "advanced.aethermind.token_env")
    if mode == "remote" and (not endpoint or not remote_project.strip()):
        raise ConfigError("Remote AetherMind needs an MCP endpoint and its owning-host project root or key")
    project = remote_project if mode == "remote" else local_project
    return AetherMindSettings(project=project, mode=mode, local_project=local_project,
                              endpoint=endpoint, token_env=token_env,
                              create_project=mode == "embedded" and not local.strip())


class Config:
    """One validated Goblin configuration. It is immutable in practice: a save
    builds a new Config and the host swaps the reference atomically."""

    def __init__(self, home, manifest, values):
        self.home = _path(home)
        self.manifest = manifest
        self.values = values
        self.agent_name = values["agent_name"]
        self.user_instructions = values["user_instructions"]
        self.workspace = values["workspace"]
        self.model_base_url = values["model_base_url"]
        self.model = values["model"]
        self.model_connection = values["model_connection"]
        self.account_models = values["account_models"]
        self.custom_models = values["custom_models"]
        self.api_kind = values["api_kind"]
        self.cli_command = values["cli_command"]
        self.reasoning = values["reasoning"]
        self.thinking_budget = values["thinking_budget"]
        self.approval = canonical_approval(values.get("approval") or "ask")
        self.world_view = values["world_view"]
        self.floor_interval_seconds = values["floor_interval_seconds"]
        self.voice_model = values["voice_model"]
        self.voice_name = values["voice_name"]
        self.voice_auth = values["voice_auth"]
        self.advanced = deepcopy(values["advanced"])
        self.tools = _tools_settings(values["advanced"].get("tools"), self.approval)
        self.skills = _skills_settings(values["advanced"].get("skills"))
        self.mcp_servers = {}
        for name, raw in (values["advanced"].get("mcp_servers") or {}).items():
            self.mcp_servers[name] = _mcp_server(name, raw)
        self.remotes = {}
        for name, raw in (values["advanced"].get("remotes") or {}).items():
            self.remotes[name] = _remote(name, raw)
        self.view = ViewSettings()
        self.compactor = _compactor_settings(values["advanced"].get("compactor"), manifest)
        self.agents = AgentsSettings()
        self.aethermind = _aethermind_settings(values["advanced"].get("aethermind"), self.home)
        self.computer_use = _computer_use_settings(values["advanced"].get("computer_use"))
        self.cua = _cua_settings(values["advanced"].get("cua"))
        resources = Path(os.environ.get("GOBLIN_RESOURCES") or Path(__file__).resolve().parents[2])
        helpers = manifest.get("native_helpers") or {}
        computer_helper = helpers.get("computer_use") or {}
        if computer_helper and "helper" not in (self.advanced.get("computer_use") or {}):
            self.computer_use.helper = str(resources / "Helpers" / (computer_helper["name"] + ".app"))
        mac_helper = helpers.get("mac_look") or {}
        if mac_helper and "mac_helper_app" not in (self.advanced.get("cua") or {}):
            self.cua.mac_helper_app = str(resources / "Helpers" / (mac_helper["name"] + ".app"))
            self.cua.mac_bundle_id = mac_helper["bundle_id"]
        self.cwd = Path(self.workspace)
        self.slug = str(manifest.get("session_slug") or "goblin")
        self.autonomy = _autonomy_settings(
            values["advanced"].get("autonomy"),
            world_view=self.world_view,
            floor_interval_seconds=self.floor_interval_seconds,
            floor_slug=self.slug,
        )
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", self.slug):
            raise ConfigError("product.json session_slug must be lowercase letters, digits and hyphens")
        self.voice = _voice_settings(self, manifest)
        self.identity_max_bytes = 65536
        self.hooks = []
        self.prompt = _PromptSettings()
        self.credentials = {}

    def operator_label(self):
        return self.manifest.get("operator_label") or "the operator"

    def operator_label_sentence(self):
        label = self.operator_label()
        if not label:
            return "The operator"
        return label[:1].upper() + label[1:]

    def floor_label(self):
        name = (self.autonomy.floor_name or "").strip()
        return name or "Autonomy"

    def backend_for(self, role="main"):
        return _BackendSettings(self, compaction=role == "compactor")

    def chat_model(self):
        return self.model_binding()[2]

    def model_binding(self, *, compaction=False):
        connection = self.model_connection
        endpoint = self.model_base_url
        model = self.model
        api_kind = self.api_kind
        cli_command = self.cli_command
        reasoning = self.reasoning
        thinking_budget = self.thinking_budget
        if connection != "custom":
            choice = self.account_models.get(connection, {})
            model = choice.get("model", "")
            reasoning = choice.get("reasoning", "low")
            thinking_budget = choice.get("thinking_budget", 0)
        if compaction:
            compactor = self.compactor
            if compactor.connection == "custom":
                connection = "custom"
                endpoint = compactor.model_base_url or self.model_base_url
                model = compactor.model
                api_kind = compactor.api_kind
                cli_command = ""
                reasoning = self.reasoning
                thinking_budget = self.thinking_budget
            elif compactor.connection:
                connection = compactor.connection
                choice = compactor.account_models.get(connection, {})
                model = choice.get("model", "")
                reasoning = choice.get("reasoning", "low")
                thinking_budget = choice.get("thinking_budget", 0)
                cli_command = ""
            elif connection == "custom":
                model = compactor.inherit_model or model
            else:
                override = compactor.account_models.get(connection, {})
                model = override.get("model") or model
        if connection != "custom":
            definition = self.manifest["oauth"][connection]
            endpoint = definition["model_base_url"]
            api_kind = definition["api_kind"]
            cli_command = ""
        return (connection, endpoint.rstrip("/"), model, api_kind, cli_command,
                self.workspace, reasoning, thinking_budget)

    @property
    def backend(self):
        return _BackendSettings(self)

    def settings_dict(self):
        return deepcopy(self.values)

    def identity_files(self):
        return list(self.manifest.get("identity_files") or [])

    def validate(self):
        return self

    def __repr__(self):
        return "Config(home=%r, slug=%r)" % (str(self.home), self.slug)


class _PromptSettings:
    body_map = True
    architecture = ""
    skill_guidance = ""
    memory_guidance = ""
    history_guidance = ""
    turn_reminder = ""
    delegation_intro = ""
    delegation_guidance = {}


class _BackendSettings:
    """The source Backend(settings) constructor shape without extra sections."""

    def __init__(self, config, *, compaction=False):
        binding = config.model_binding(compaction=compaction)
        connection, endpoint, model, api_kind, command, workspace, _, _ = binding
        self.name = "default"
        self.base_url = endpoint
        self.model = model
        self.api_key_env = ""
        self.extra = {"provider_kind": _provider_kind(api_kind),
                      "connection": connection,
                      "cli": {"vendor": _cli_vendor(command), "command": command, "cwd": str(workspace)}}


def _provider_kind(api_kind):
    return {"chat_completions": "openai_chat", "responses": "openai_responses",
            "codex_responses": "codex_responses", "cli_worker": "cli_worker"}[api_kind]


def _cli_vendor(command):
    if not command:
        return ""
    name = Path(command).name.lower()
    for vendor in ("codex", "claude", "grok"):
        if name == vendor or name.startswith(vendor + "."):
            return vendor
    return ""


def canonical_approval(value):
    """Map a stored approval mode onto ask or unleashed.

    Older files used safe and yolo. Those values still load.
    """
    if value == "safe":
        return "ask"
    if value == "yolo":
        return "unleashed"
    return value


def _tools_settings(raw, approval):
    base = ToolsSettings()
    approval = canonical_approval(approval)
    base.approval = "unleashed" if approval == "unleashed" else "ask"
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ConfigError("advanced.tools must be an object")
    known = {
        "approval_classes", "approval_rules", "disabled", "clip_chars", "timeout",
        "list_limit", "grep_limit", "grep_scan", "git_log_limit", "spill_dir",
        "output_tail_chars", "read_line_limit", "glob_limit", "read_bytes_limit",
        "image_bytes_limit", "fetch_max_bytes", "fetch_timeout", "shell_max_timeout",
    }
    extra = set(raw) - known
    if extra:
        raise ConfigError("advanced.tools has unknown fields: " + ", ".join(sorted(extra)))
    if "approval_classes" in raw:
        if not isinstance(raw["approval_classes"], dict):
            raise ConfigError("advanced.tools.approval_classes must be an object")
        for label, patterns in raw["approval_classes"].items():
            _string_list(patterns, "advanced.tools.approval_classes." + str(label))
        base.approval_classes = deepcopy(raw["approval_classes"])
    if "approval_rules" in raw:
        if not isinstance(raw["approval_rules"], dict):
            raise ConfigError("advanced.tools.approval_rules must be an object")
        for label, rule in raw["approval_rules"].items():
            if rule not in ("allow", "gate"):
                raise ConfigError("advanced.tools.approval_rules." + str(label) + " must be allow or gate")
        base.approval_rules = dict(raw["approval_rules"])
    if "disabled" in raw:
        base.disabled = _string_list(raw["disabled"], "advanced.tools.disabled")
    for name, minimum, maximum in (
        ("clip_chars", 1000, 1000000), ("list_limit", 1, 2000), ("grep_limit", 1, 500),
        ("grep_scan", 1, 100000), ("git_log_limit", 1, 100), ("output_tail_chars", 1, 1000000),
        ("read_line_limit", 1, 100000), ("glob_limit", 1, 2000), ("read_bytes_limit", 1, 100000000),
        ("image_bytes_limit", 1, 100000000), ("fetch_max_bytes", 1, 100000000),
    ):
        if name in raw:
            setattr(base, name, _whole(raw[name], "advanced.tools." + name, minimum, maximum))
    for name, maximum in (("timeout", 600.0), ("fetch_timeout", 120.0), ("shell_max_timeout", 600.0)):
        if name in raw:
            value = _number(raw[name], "advanced.tools." + name, 0.001)
            if value > maximum:
                raise ConfigError("advanced.tools." + name + " is too large")
            setattr(base, name, value)
    if "spill_dir" in raw:
        base.spill_dir = _text(raw["spill_dir"], "advanced.tools.spill_dir", allow_empty=False)
    return base


def _skills_settings(raw):
    base = SkillsSettings()
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ConfigError("advanced.skills must be an object")
    known = {"enabled", "shelf", "trusted_projects", "project_dirs", "note_bytes",
             "note_description_chars", "search_limit", "disabled", "extra_dirs"}
    extra = set(raw) - known
    if extra:
        raise ConfigError("advanced.skills has unknown fields: " + ", ".join(sorted(extra)))
    if "enabled" in raw:
        base.enabled = _bool(raw["enabled"], "advanced.skills.enabled")
    if "shelf" in raw:
        base.shelf = _text(raw["shelf"], "advanced.skills.shelf")
    for name in ("trusted_projects", "project_dirs", "disabled", "extra_dirs"):
        if name in raw:
            setattr(base, name, _string_list(raw[name], "advanced.skills." + name))
    for name, minimum in (("note_bytes", 100), ("note_description_chars", 20),
                          ("search_limit", 1)):
        if name in raw:
            setattr(base, name, _whole(raw[name], "advanced.skills." + name, minimum))
    return base


def _mcp_server(name, raw):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ConfigError("advanced.mcp_servers names must be 1..64 letters, numbers, underscores or hyphens")
    if not isinstance(raw, dict):
        raise ConfigError("advanced.mcp_servers." + name + " must be an object")
    known = {"enabled", "description", "url", "command", "env", "cwd", "token_env",
             "timeout", "list_ttl", "include", "exclude", "safe_tools", "tool_prefix",
             "session_tools"}
    extra = set(raw) - known
    if extra:
        raise ConfigError("advanced.mcp_servers." + name + " has unknown fields: " + ", ".join(sorted(extra)))
    _check_secret_keys(raw, "advanced.mcp_servers." + name)
    server = McpServerConfig(name=name)
    if raw.get("url"):
        server.url = _url(raw["url"], "advanced.mcp_servers." + name + ".url")
    if raw.get("command") is not None:
        server.command = _string_list(raw["command"], "advanced.mcp_servers." + name + ".command")
    if not server.url and not server.command:
        raise ConfigError("advanced.mcp_servers." + name + " needs url or command")
    if server.url and server.command:
        raise ConfigError("advanced.mcp_servers." + name + " cannot set both url and command")
    if "enabled" in raw:
        server.enabled = _bool(raw["enabled"], "advanced.mcp_servers." + name + ".enabled")
    if "session_tools" in raw:
        server.session_tools = _bool(raw["session_tools"], "advanced.mcp_servers." + name + ".session_tools")
    if "description" in raw:
        server.description = _text(raw["description"], "advanced.mcp_servers." + name + ".description")
    if "cwd" in raw:
        server.cwd = _text(raw["cwd"], "advanced.mcp_servers." + name + ".cwd")
    if "token_env" in raw:
        server.token_env = _text(raw["token_env"], "advanced.mcp_servers." + name + ".token_env")
    if "env" in raw:
        if not isinstance(raw["env"], dict):
            raise ConfigError("advanced.mcp_servers." + name + ".env must be an object")
        for key, value in raw["env"].items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ConfigError("advanced.mcp_servers." + name + ".env must map strings to strings")
        server.env = dict(raw["env"])
    for field_name in ("include", "exclude", "safe_tools"):
        if field_name in raw:
            setattr(server, field_name, _string_list(raw[field_name], "advanced.mcp_servers." + name + "." + field_name))
    if "tool_prefix" in raw:
        server.tool_prefix = _text(raw["tool_prefix"], "advanced.mcp_servers." + name + ".tool_prefix")
    for field_name, minimum, maximum in (("timeout", 0.001, 600.0), ("list_ttl", 0.0, 86400.0)):
        if field_name in raw:
            value = _number(raw[field_name], "advanced.mcp_servers." + name + "." + field_name, minimum)
            if value > maximum:
                raise ConfigError("advanced.mcp_servers." + name + "." + field_name + " is too large")
            setattr(server, field_name, value)
    return server


def _remote(name, raw):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ConfigError("advanced.remotes names must be 1..64 letters, numbers, underscores or hyphens")
    if not isinstance(raw, dict):
        raise ConfigError("advanced.remotes." + name + " must be an object")
    known = {"enabled", "description", "host", "port", "user", "identity_file",
             "known_hosts_file", "strict_host_key_checking", "ssh_command",
             "connect_timeout", "timeout", "desk", "desk_screenshot_command",
             "desk_input_command"}
    extra = set(raw) - known
    if extra:
        raise ConfigError("advanced.remotes." + name + " has unknown fields: " + ", ".join(sorted(extra)))
    _check_secret_keys(raw, "advanced.remotes." + name)
    remote = RemoteConfig(name=name)
    if "enabled" in raw:
        remote.enabled = _bool(raw["enabled"], "advanced.remotes." + name + ".enabled")
    for field_name in ("description", "host", "user", "identity_file", "known_hosts_file"):
        if field_name in raw:
            setattr(remote, field_name, _text(raw[field_name], "advanced.remotes." + name + "." + field_name))
    if "strict_host_key_checking" in raw:
        value = _text(raw["strict_host_key_checking"], "advanced.remotes." + name + ".strict_host_key_checking")
        if value not in ("yes", "no", "accept-new"):
            raise ConfigError("advanced.remotes." + name + ".strict_host_key_checking must be yes, no, or accept-new")
        remote.strict_host_key_checking = value
    if "ssh_command" in raw:
        remote.ssh_command = _string_list(raw["ssh_command"], "advanced.remotes." + name + ".ssh_command")
    if "port" in raw:
        remote.port = _whole(raw["port"], "advanced.remotes." + name + ".port", 1, 65535)
    if "connect_timeout" in raw:
        remote.connect_timeout = _whole(raw["connect_timeout"], "advanced.remotes." + name + ".connect_timeout", 1, 300)
    if "timeout" in raw:
        remote.timeout = _number(raw["timeout"], "advanced.remotes." + name + ".timeout", 0.001)
    if "desk" in raw:
        remote.desk = _bool(raw["desk"], "advanced.remotes." + name + ".desk")
    if "desk_screenshot_command" in raw:
        remote.desk_screenshot_command = _text(
            raw["desk_screenshot_command"], "advanced.remotes." + name + ".desk_screenshot_command")
    if "desk_input_command" in raw:
        remote.desk_input_command = _text(
            raw["desk_input_command"], "advanced.remotes." + name + ".desk_input_command")
    if not remote.host:
        raise ConfigError("advanced.remotes." + name + ".host is required")
    return remote



def _model_connection(value, manifest, field, *, inherit=False):
    connection = _text(value, field)
    if inherit and connection == "":
        return connection
    if connection == "custom" or connection in (manifest.get("oauth") or {}):
        return connection
    raise ConfigError(field + " must select a configured account or custom endpoint")


def _compactor_settings(raw, manifest):
    base = CompactorSettings()
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ConfigError("advanced.compactor must be an object")
    known = {
        "workers", "retries", "context_bytes",
        "model", "model_base_url", "api_kind", "connection", "inherit_model",
        "account_models",
    }
    extra = set(raw) - known
    if extra:
        raise ConfigError("advanced.compactor has unknown fields: " + ", ".join(sorted(extra)))
    if "enabled" in raw:
        if raw["enabled"] is not True:
            raise ConfigError("advanced.compactor.enabled is mandatory")
    if "workers" in raw:
        base.workers = _whole(raw["workers"], "advanced.compactor.workers", 1, 32)
    if "retries" in raw:
        base.retries = _whole(raw["retries"], "advanced.compactor.retries", 0, 10)
    if "context_bytes" in raw:
        base.context_bytes = _whole(raw["context_bytes"], "advanced.compactor.context_bytes", 1000, 1000000)
    if "model" in raw:
        base.model = _text(raw["model"], "advanced.compactor.model")
    if "connection" in raw:
        base.connection = _model_connection(
            raw["connection"], manifest, "advanced.compactor.connection", inherit=True)
    if "inherit_model" in raw:
        base.inherit_model = _text(
            raw["inherit_model"], "advanced.compactor.inherit_model")
    if "account_models" in raw:
        base.account_models = _account_models(
            raw["account_models"], manifest, "advanced.compactor.account_models")
    if "model_base_url" in raw:
        # Match Backend: validate like the main endpoint, then drop a trailing slash.
        url = _url(raw["model_base_url"], "advanced.compactor.model_base_url")
        base.model_base_url = url.rstrip("/") if url else ""
    if "api_kind" in raw:
        kind = _text(raw["api_kind"], "advanced.compactor.api_kind", allow_empty=False)
        if kind not in ("chat_completions", "responses"):
            raise ConfigError(
                "advanced.compactor.api_kind must be chat_completions or responses"
            )
        base.api_kind = kind

    if base.connection == "custom" and not base.model.strip():
        raise ConfigError("advanced.compactor.connection requires advanced.compactor.model")
    if base.connection and base.connection != "custom":
        if not base.account_models.get(base.connection, {}).get("model", "").strip():
            raise ConfigError(
                "advanced.compactor.connection requires advanced.compactor.account_models."
                + base.connection)
    return base


def _account_models(raw, manifest, field):
    """Validate the model options owned by each account connection."""
    if not isinstance(raw, dict):
        raise ConfigError(field + " must be an object")
    accounts = manifest.get("oauth") or {}
    slots = {}
    for provider, value in raw.items():
        if provider not in accounts:
            raise ConfigError(field + " has unknown account: " + str(provider))
        path = field + "." + str(provider)
        if not isinstance(value, dict):
            raise ConfigError(path + " must be an object")
        unknown = set(value) - {"model", "reasoning", "thinking_budget"}
        if unknown:
            raise ConfigError(path + " has unknown fields: " + ", ".join(sorted(unknown)))
        reasoning = value.get("reasoning", "low")
        if reasoning not in REASONING_LEVELS:
            raise ConfigError(path + ".reasoning must be off, low, medium, or high")
        slots[provider] = {
            "model": _text(value.get("model", ""), path + ".model"),
            "reasoning": reasoning,
            "thinking_budget": _whole(value.get("thinking_budget", 0),
                                      path + ".thinking_budget", 0, 2000000),
        }
    return slots


def _custom_models(raw):
    """Reusable custom route values. Active role fields remain authoritative."""
    if not isinstance(raw, dict):
        raise ConfigError('custom_models must be an object')
    profiles = {}
    fields = {'name', 'model_base_url', 'model', 'api_kind', 'cli_command',
              'reasoning', 'thinking_budget'}
    for identifier, value in raw.items():
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identifier):
            raise ConfigError('custom_models IDs must be 1..64 letters, numbers, underscores, or hyphens')
        path = 'custom_models.' + identifier
        if not isinstance(value, dict):
            raise ConfigError(path + ' must be an object')
        unknown = set(value) - fields
        if unknown:
            raise ConfigError(path + ' has unknown fields: ' + ', '.join(sorted(unknown)))
        api_kind = value.get('api_kind', 'chat_completions')
        if api_kind not in API_KINDS:
            raise ConfigError(path + '.api_kind must be chat_completions, responses, or cli_worker')
        reasoning = value.get('reasoning', 'low')
        if reasoning not in REASONING_LEVELS:
            raise ConfigError(path + '.reasoning must be off, low, medium, or high')
        profiles[identifier] = {
            'name': _text(value.get('name', ''), path + '.name', allow_empty=False),
            'model_base_url': _url(value.get('model_base_url', ''), path + '.model_base_url'),
            'model': _text(value.get('model', ''), path + '.model'),
            'api_kind': api_kind,
            'cli_command': _text(value.get('cli_command', ''), path + '.cli_command'),
            'reasoning': reasoning,
            'thinking_budget': _whole(value.get('thinking_budget', 0), path + '.thinking_budget', 0, 2000000),
        }
    return profiles


def _autonomy_settings(raw, *, world_view, floor_interval_seconds, floor_slug):
    base = AutonomySettings()
    base.world_view = world_view or ""
    base.floor_slug = floor_slug
    base.self_prompt_gap_seconds = float(floor_interval_seconds)
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ConfigError("advanced.autonomy must be an object")
    raw = dict(raw)
    raw.pop("infra_poll_seconds", None)
    known = {
        "enabled", "state_file", "tasks_dir", "floor_file", "tick_seconds", "min_wake_gap_seconds",
        "max_wakes_per_hour", "max_wakes_per_day", "max_timers", "min_timer_seconds",
        "max_timer_seconds", "max_tasks", "task_max_seconds", "task_stall_seconds",
        "keep_finished_tasks", "wake_output_chars", "approval_expire_seconds",
        "self_prompt_enabled", "self_prompt_gap_seconds",
        "standing_floor", "floor_slug", "floor_name", "world_view",
        "lesson_file", "lesson_pass_score", "max_open_lessons", "lesson_fail_limit",
    }
    extra = set(raw) - known
    if extra:
        raise ConfigError("advanced.autonomy has unknown fields: " + ", ".join(sorted(extra)))
    bools = (
        "enabled", "self_prompt_enabled", "standing_floor",
    )
    for name in bools:
        if name in raw:
            setattr(base, name, _bool(raw[name], "advanced.autonomy." + name))
    texts = (
        "state_file", "tasks_dir", "floor_file", "floor_slug", "floor_name", "world_view",
        "lesson_file",
    )
    for name in texts:
        if name in raw:
            setattr(base, name, _text(raw[name], "advanced.autonomy." + name))
    wholes = (
        ("max_wakes_per_hour", 0, 100000), ("max_wakes_per_day", 0, 100000),
        ("max_timers", 1, 1000), ("max_tasks", 1, 100), ("keep_finished_tasks", 0, 10000),
        ("wake_output_chars", 100, 100000),
        ("max_open_lessons", 1, 1000), ("lesson_fail_limit", 1, 100),
    )
    for name, minimum, maximum in wholes:
        if name in raw:
            setattr(base, name, _whole(raw[name], "advanced.autonomy." + name, minimum, maximum))
    numbers = (
        "tick_seconds", "min_wake_gap_seconds", "min_timer_seconds", "max_timer_seconds",
        "task_max_seconds", "task_stall_seconds",
        "approval_expire_seconds", "self_prompt_gap_seconds",
        "lesson_pass_score",
    )
    for name in numbers:
        if name in raw:
            setattr(base, name, _number(raw[name], "advanced.autonomy." + name, 0.0))
    # Top-level floor_interval remains the operator-facing gap control when autonomy
    # does not override self_prompt_gap_seconds explicitly.
    if "self_prompt_gap_seconds" not in (raw or {}):
        base.self_prompt_gap_seconds = float(floor_interval_seconds)
    if not base.floor_slug:
        base.floor_slug = floor_slug
    if "world_view" not in (raw or {}) and world_view:
        base.world_view = world_view
    return base


def _cua_settings(raw):
    base = CuaSettings()
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ConfigError("advanced.cua must be an object")
    known = {
        "mac_enabled", "mac_helper_app", "mac_bundle_id", "swiftc", "codesign_identity",
        "screencapture", "sips", "max_image_px", "attach_images", "timeout", "backend",
        "screenshot_scale", "osascript", "input_command", "window_command",
        "screenshot_commands",
    }
    extra = set(raw) - known
    if extra:
        raise ConfigError("advanced.cua has unknown fields: " + ", ".join(sorted(extra)))
    if "mac_enabled" in raw:
        base.mac_enabled = _bool(raw["mac_enabled"], "advanced.cua.mac_enabled")
    if "attach_images" in raw:
        base.attach_images = _bool(raw["attach_images"], "advanced.cua.attach_images")
    for name in (
        "mac_helper_app", "mac_bundle_id", "swiftc", "codesign_identity", "screencapture",
        "sips", "backend", "osascript", "input_command", "window_command",
    ):
        if name in raw:
            setattr(base, name, _text(raw[name], "advanced.cua." + name))
    if base.backend not in ("auto", "mac", "linux", "fake", "unsupported"):
        raise ConfigError("advanced.cua.backend must be auto, mac, linux, fake, or unsupported")
    if "max_image_px" in raw:
        base.max_image_px = _whole(raw["max_image_px"], "advanced.cua.max_image_px", 64, 10000)
    if "timeout" in raw:
        base.timeout = _number(raw["timeout"], "advanced.cua.timeout", 0.1)
    if "screenshot_scale" in raw:
        base.screenshot_scale = _number(raw["screenshot_scale"], "advanced.cua.screenshot_scale", 0.1)
    if "screenshot_commands" in raw:
        base.screenshot_commands = _string_list(raw["screenshot_commands"], "advanced.cua.screenshot_commands")
    return base


def _computer_use_settings(raw):
    base = ComputerUseSettings()
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ConfigError("advanced.computer_use must be an object")
    known = {
        "backend", "provider", "tool_prefix", "text_chars", "helper", "timeout",
    }
    extra = set(raw) - known
    if extra:
        raise ConfigError("advanced.computer_use has unknown fields: " + ", ".join(sorted(extra)))
    for name in ("backend", "provider", "tool_prefix", "helper"):
        if name in raw:
            setattr(base, name, _text(raw[name], "advanced.computer_use." + name))
    if base.backend not in ("auto", "native", "mcp", "linux", "mac", "fake", "unsupported"):
        raise ConfigError("advanced.computer_use.backend must be auto, native, mcp, linux, mac, fake, or unsupported")
    if "text_chars" in raw:
        base.text_chars = _whole(raw["text_chars"], "advanced.computer_use.text_chars", 64, 1000000)
    if "timeout" in raw:
        base.timeout = _number(raw["timeout"], "advanced.computer_use.timeout", 0.1)
    return base


def _voice_settings(config, manifest):
    raw = manifest.get("voice") or {}
    voices = [config.voice_name] if config.voice_name else []
    return VoiceSettings(
        websocket_url=str(raw.get("websocket_url") or ""),
        api_base_url=str(raw.get("api_base_url") or ""),
        client_secret_path=str(raw.get("client_secret_path") or ""),
        client_protocol_prefix=str(raw.get("client_protocol_prefix") or ""),
        model=config.voice_model,
        voices=voices,
        voice=config.voice_name,
        audio_sample_rate=int(raw.get("sample_rate") or 24000),
        dedicated_slug=config.slug,
        dedicated_title=str(manifest.get("name") or "Goblin"),
        dedicated_reasoning="low",
        tools_mode="handoff",
        auth=config.voice_auth,
        session_options={"audio": {"input": {}, "output": {}}},
        turn_detection={"type": "server_vad", "idle_timeout_ms": 800},
    )


def default_settings(manifest):
    model = manifest.get("default_model") or {}
    voice = manifest.get("voice") or {}
    return {
        "agent_name": str(manifest.get("name") or "Goblin"),
        "user_instructions": "",
        "workspace": "",
        "model_base_url": str(model.get("base_url") or ""),
        "model": str(model.get("model") or ""),
        "model_connection": "custom",
        "api_kind": str(model.get("api_kind") or "chat_completions"),
        "cli_command": "",
        "reasoning": "low",
        "thinking_budget": 0,
        "approval": "ask",
        "world_view": "",
        "floor_interval_seconds": 900.0,
        "voice_model": str(voice.get("model") or ""),
        "voice_name": str(voice.get("voice") or ""),
        "voice_auth": "oauth",
        "advanced": {},
        "account_models": {},
        "custom_models": {},
    }


def _migrate_route_models(raw, manifest):
    """Preserve legacy active routes without rewriting their custom fields."""
    values = deepcopy(raw)
    accounts = manifest.get("oauth") or {}
    connection = values.get("model_connection") or "custom"
    model = values.get("model") or ""
    choice = {"model": model, "reasoning": values.get("reasoning", "low"),
              "thinking_budget": values.get("thinking_budget", 0)}
    if "account_models" not in values:
        slots = {}
        if connection in accounts and model:
            slots[connection] = choice
        values["account_models"] = slots
    advanced = values.get("advanced")
    if isinstance(advanced, dict):
        compactor = advanced.get("compactor")
        if isinstance(compactor, dict):
            compactor_connection = compactor.get("connection") or ""
            compactor_model = compactor.get("model") or ""
            legacy = "account_models" not in compactor
            if legacy:
                if compactor.get("model_base_url") and not compactor_model:
                    raise ConfigError(
                        "advanced.compactor.model_base_url requires advanced.compactor.model")
                compactor_slots = {}
                provider = compactor_connection or connection
                if provider in accounts and compactor_model:
                    compactor_slots[provider] = {
                        "model": compactor_model, "reasoning": "low", "thinking_budget": 0}
                compactor["account_models"] = compactor_slots
            if legacy and compactor_connection == "" and compactor_model and connection == "custom":
                if compactor.get("model_base_url"):
                    compactor["connection"] = "custom"
                if "api_kind" not in compactor:
                    compactor["api_kind"] = values.get("api_kind", "chat_completions")
                if "inherit_model" not in compactor:
                    compactor["inherit_model"] = compactor_model
    return values


def normalize_settings(raw, manifest, home):
    """Validate a settings object, fill defaults, and return a plain dict."""
    if not isinstance(raw, dict):
        raise ConfigError("settings must be an object")
    raw = _migrate_route_models(raw, manifest)
    _check_secret_keys(raw, "settings")
    unknown = set(raw) - set(SETTINGS_KEYS)
    if unknown:
        raise ConfigError("settings has unknown fields: " + ", ".join(sorted(unknown)))
    values = default_settings(manifest)
    values.update(deepcopy(raw))
    _text(values["agent_name"], "agent_name", allow_empty=False)
    if len(values["agent_name"]) > 200:
        raise ConfigError("agent_name must be at most 200 characters")
    _text(values["user_instructions"], "user_instructions")
    workspace = _text(values["workspace"], "workspace")
    if workspace:
        workspace = str(_path(workspace))
    else:
        workspace = str((_path(home) / "workspace").resolve())
    values["workspace"] = workspace
    values["model_base_url"] = _url(values["model_base_url"], "model_base_url")
    values["model"] = _text(values["model"], "model")
    values["model_connection"] = _model_connection(
        values["model_connection"], manifest, "model_connection")
    values["account_models"] = _account_models(
        values["account_models"], manifest, "account_models")
    values["custom_models"] = _custom_models(values["custom_models"])
    if values["api_kind"] not in API_KINDS:
        raise ConfigError("api_kind must be chat_completions, responses, or cli_worker")
    values["cli_command"] = _text(values["cli_command"], "cli_command")
    if values["reasoning"] not in REASONING_LEVELS:
        raise ConfigError("reasoning must be off, low, medium, or high")
    values["thinking_budget"] = _whole(values["thinking_budget"], "thinking_budget", 0, 2000000)
    values["approval"] = canonical_approval(values["approval"])
    if values["approval"] not in APPROVALS:
        raise ConfigError("approval must be ask or unleashed")
    values["world_view"] = _text(values["world_view"], "world_view")
    values["floor_interval_seconds"] = _number(values["floor_interval_seconds"], "floor_interval_seconds", 5.0)
    if values["floor_interval_seconds"] > 7 * 24 * 3600:
        raise ConfigError("floor_interval_seconds is too large")
    values["voice_model"] = _text(values["voice_model"], "voice_model")
    values["voice_name"] = _text(values["voice_name"], "voice_name")
    if values["voice_auth"] not in VOICE_AUTHS:
        raise ConfigError("voice_auth must be oauth or api_key")
    advanced = values["advanced"]
    if not isinstance(advanced, dict):
        raise ConfigError("advanced must be an object")
    extra = set(advanced) - set(ADVANCED_SECTIONS)
    if extra:
        raise ConfigError("advanced has unknown sections: " + ", ".join(sorted(extra)))
    values["advanced"] = deepcopy(advanced)
    # Build once so the full advanced model validates before any write.
    Config(home, manifest, values)
    return values


def load_manifest(path):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError("product manifest could not be read: " + type(error).__name__)
    if not isinstance(raw, dict):
        raise ConfigError("product manifest must be an object")
    for key in ("name", "session_slug", "keychain_services"):
        if key not in raw:
            raise ConfigError("product manifest is missing " + key)
    return raw


def load_config(home, manifest_path):
    """Load the saved settings, or defaults. A blank model is valid."""
    manifest = load_manifest(manifest_path)
    path = Path(home) / "config.json"
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigError("settings file could not be read: " + type(error).__name__)
    else:
        raw = default_settings(manifest)
    values = normalize_settings(raw, manifest, home)
    return Config(home, manifest, values)


def write_settings(home, manifest_path, raw):
    """Validate, adopt the workspace, then persist atomically.

    The workspace is created before the settings document is written, so a bad
    path cannot replace a working saved setting and block the next launch.
    Nothing is adopted until the complete new document is on disk and fsynced.
    """
    manifest = load_manifest(manifest_path)
    values = normalize_settings(raw, manifest, home)
    issue = workspace_issue(values["workspace"], create=True)
    if issue:
        raise ConfigError("workspace: " + issue)
    path = Path(home) / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    encoded = json.dumps(values, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return Config(home, manifest, values)
