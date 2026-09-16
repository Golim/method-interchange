from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml


@dataclass
class Config:
    """
    Configuration container for the MI detection crawler.

    Holds all tunable parameters with typed attributes for type safety.
    """
    # Crawling parameters
    max_pages: int = 10
    max_depth: int = 5
    page_load_timeout: int = 30000
    sleep_between_pages: int = 0
    only_internal: bool = True

    # Browser parameters
    headless: bool = True
    browser_args: List[str] = field(default_factory=lambda: [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage"
    ])

    # Browser-use system Chromium/Chrome (auth_agent) parameters
    browser_use_chrome_headless: bool = False
    browser_use_chrome_executable_path: Optional[str] = "chromium"
    browser_use_chrome_user_data_dir: Optional[str] = "~/.config/chromium"
    browser_use_chrome_profile_directory: str = "Default"
    browser_use_chrome_persist_user_data_dir: Optional[str] = "~/.cache/method-interchangeability/browser-use-user-data-dir-persistent"

    # Capture parameters
    deduplicate: bool = True
    blacklisted_domains: List[str] = field(default_factory=list)
    blacklisted_url_patterns: List[str] = field(default_factory=list)
    dedup_bypass_patterns: List[str] = field(default_factory=list)
    max_posts: int = 100  # Max POST requests to capture per site (0 = unlimited)

    # Output parameters
    output_directory: str = "output"

    # Logging parameters
    log_level: str = "INFO"
    error_log_dir: str = "logs"

    # Detection parameters
    similarity_threshold: float = 95.0  # % above which responses are interchangeable
    array_style: str = "php"  # "php" | "numbered" | "repeated"
    max_json_depth: int = 2  # Max nesting depth for JSON flattening
    request_timeout: int = 30  # Timeout for GET requests in seconds
    rate_limit_delay: float = 1.0  # Delay between GET requests in seconds
    enable_normalization: bool = False  # Opt-in: normalize dynamic content before comparison

    # Batch parameters
    inter_site_delay: float = 5.0  # Politeness delay between sites in seconds
    batch_mode: str = "full"  # Default batch mode: crawl, detect, full
    browser_restart: bool = True  # Whether to restart browser between sites (clean state)
    site_timeout: int = 900  # Max seconds for one auth-crawl-batch site before killing child process

    # Form interaction parameters
    max_clickable: int = 10  # Max standalone buttons to click per page (0 = disable)
    max_forms: int = 5  # Max forms to interact with per page (0 = disable)
    max_form_fields: int = 20  # Max inputs/selects to touch per form
    form_action_timeout: int = 1000  # Timeout for individual form actions in milliseconds
    form_interaction_timeout: int = 15000  # Overall form/button interaction budget per page in milliseconds
    dangerous_action_patterns: List[str] = field(default_factory=lambda: [
        "logout", "log out", "sign out", "delete", "remove",
        "cancel", "unsubscribe", "deactivate",
        "transfer", "payment", "admin", "destroy", "password-reset"
    ])

    # Agent crawl parameters
    agent_model: str = "qwen3-vl:latest"
    agent_num_ctx: int = 8192
    agent_max_steps: int = 50
    agent_think: Optional[Any] = None  # think budget: False, 'low', 'medium', 'high', or None (model default)
    agent_use_vision: bool = False

    # Provider abstraction fields (Phase 10.1)
    agent_provider: str = "ollama"
    agent_ollama_model: str = "qwen2.5:7b"
    agent_ollama_num_ctx: int = 8192
    agent_ollama_think: Optional[Any] = None
    agent_copilot_model: str = "gpt-4o"
    agent_google_model: str = "gemini-2.5-flash"
    agent_openrouter_model: str = "google/gemma-4-26b-a4b-it:free"
    agent_openrouter_base_url: str = "https://openrouter.ai/api/v1"
    agent_openrouter_api_key: Optional[str] = None
    agent_azure_model: str = "gpt-4.1"
    agent_azure_deployment: Optional[str] = None
    agent_azure_api_version: Optional[str] = None
    agent_vertex_model: str = "gemini-2.5-flash"
    agent_vertex_project: Optional[str] = None
    agent_vertex_location: Optional[str] = None

    # Azure OpenAI fields (optional)
    # Endpoint should be your Azure OpenAI resource URL, e.g. https://your-resource.openai.azure.com
    agent_azure_base_url: Optional[str] = None
    # deployment name for the model you deployed (e.g. "gpt-5-nano")
    agent_azure_deployment: Optional[str] = None
    # Optionally specify API key here; it's often provided via env var in production
    agent_azure_api_key: Optional[str] = None


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deep merge two dictionaries.

    Values from override take precedence over base.
    Nested dictionaries are merged recursively.

    Args:
        base: Base dictionary
        override: Dictionary with override values

    Returns:
        Merged dictionary
    """
    result = base.copy()

    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value

    return result


def load_config(config_path: Optional[str] = None, cli_overrides: Optional[Dict[str, Any]] = None) -> Config:
    """
    Load configuration from default YAML, optional user config, and CLI overrides.

    Configuration precedence (highest to lowest):
    1. CLI overrides (--max-pages 100)
    2. User config file (if provided)
    3. Default config (config/default.yaml)

    Args:
        config_path: Optional path to user config YAML file
        cli_overrides: Optional dictionary of CLI parameter overrides

    Returns:
        Config instance with merged configuration

    Example:
        # Load defaults only
        config = load_config()

        # Load with user config
        config = load_config("my_config.yaml")

        # Load with CLI overrides
        config = load_config(cli_overrides={"max_pages": 100})
    """
    # Load default config
    default_config_path = Path(__file__).parent.parent / "config" / "default.yaml"

    try:
        with open(default_config_path, 'r') as f:
            config_dict = yaml.safe_load(f)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Default configuration file not found at {default_config_path}. "
            "Ensure the tool is properly installed and config/default.yaml exists."
        )
    except yaml.YAMLError as e:
        raise ValueError(f"Error parsing default.yaml: {e}")

    # Merge user config if provided
    if config_path:
        user_config_path = Path(config_path)
        if not user_config_path.exists():
            raise FileNotFoundError(
                f"User configuration file not found: {user_config_path}. "
                "Check the --config path or remove the flag to use defaults."
            )

        try:
            with open(user_config_path, 'r') as f:
                user_config = yaml.safe_load(f)
                # yaml.safe_load returns None for empty files
                if user_config:
                    config_dict = _deep_merge(config_dict, user_config)
        except yaml.YAMLError as e:
            raise ValueError(f"Error parsing {user_config_path}: {e}")

    # Apply CLI overrides if provided
    if cli_overrides:
        config_dict = _deep_merge(config_dict, cli_overrides)

    # Extract values from nested structure and create Config instance
    crawling = config_dict.get('crawling', {})
    browser = config_dict.get('browser', {})
    capture = config_dict.get('capture', {})
    output = config_dict.get('output', {})
    logging = config_dict.get('logging', {})
    detection = config_dict.get('detection', {})
    batch = config_dict.get('batch', {})
    browser_use_chrome = config_dict.get('browser_use_chrome', {})
    agent = config_dict.get('agent', {})
    ollama_sub = agent.get('ollama', {})
    copilot_sub = agent.get('copilot', {})
    google_sub = agent.get('google', {})
    openrouter_sub = agent.get('openrouter', {})
    azure_sub = agent.get('azure', {})
    vertex_sub = agent.get('vertex', {})

    # Backward compat: if flat 'model' key is present at agent level, it is an explicit
    # user override from the old YAML format (agent: model: <name>). Treat as ollama provider
    # and use this model name directly. This handles old configs that predate the nested structure.
    # The 'ollama' sub-dict may be present from default.yaml merge — that's fine; flat 'model'
    # takes priority when present.
    if 'model' in agent:
        agent_provider = 'ollama'
        agent_ollama_model = agent['model']
    else:
        agent_provider = agent.get('provider', 'ollama')
        agent_ollama_model = ollama_sub.get('model', 'qwen2.5:7b')

    # For num_ctx and think: if flat keys present at agent level (old format), prefer those;
    # otherwise use ollama sub-dict values (new format).
    if 'num_ctx' in agent:
        agent_ollama_num_ctx = agent['num_ctx']
    else:
        agent_ollama_num_ctx = ollama_sub.get('num_ctx', 8192)
    if 'think' in agent:
        agent_ollama_think = agent['think']
    else:
        agent_ollama_think = ollama_sub.get('think', None)
    agent_copilot_model = copilot_sub.get('model', 'gpt-4o')
    agent_google_model = google_sub.get('model', 'gemini-2.5-flash')
    agent_openrouter_model = openrouter_sub.get('model', 'openai/gpt-4.1-mini')
    agent_openrouter_base_url = openrouter_sub.get('base_url', 'https://openrouter.ai/api/v1')
    agent_openrouter_api_key = openrouter_sub.get('api_key') or None
    agent_azure_model = azure_sub.get('model', 'gpt-4.1')
    agent_vertex_model = vertex_sub.get('model', 'gemini-2.5-flash')
    agent_vertex_project = vertex_sub.get('project', None)
    agent_vertex_location = vertex_sub.get('location', None)

    # Azure specific fields
    agent_azure_base_url = azure_sub.get('base_url') or azure_sub.get('endpoint') or None
    agent_azure_api_key = azure_sub.get('api_key') or None
    agent_azure_deployment = azure_sub.get('deployment') or None
    agent_azure_api_version = azure_sub.get('api_version') or None
    # Resolve the "active" model name for backward compat
    if agent_provider == 'copilot':
        resolved_agent_model = agent_copilot_model
    elif agent_provider == 'google':
        resolved_agent_model = agent_google_model
    elif agent_provider == 'openrouter':
        resolved_agent_model = agent_openrouter_model
    elif agent_provider == 'azure':
        resolved_agent_model = agent_azure_deployment or agent_azure_model
    elif agent_provider == 'vertex':
        resolved_agent_model = agent_vertex_model
    else:
        resolved_agent_model = agent_ollama_model

    config = Config(
        max_pages=crawling.get('max_pages', 10),
        max_depth=crawling.get('max_depth', 5),
        page_load_timeout=crawling.get('page_load_timeout', 30000),
        sleep_between_pages=crawling.get('sleep_between_pages', 0),
        only_internal=crawling.get('only_internal', True),
        headless=browser.get('headless', True),
        browser_args=browser.get('args', [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage"
        ]),
        browser_use_chrome_headless=browser_use_chrome.get('headless', False),
        browser_use_chrome_executable_path=browser_use_chrome.get('executable_path', "chromium"),
        browser_use_chrome_user_data_dir=browser_use_chrome.get('user_data_dir', "~/.config/chromium"),
        browser_use_chrome_profile_directory=browser_use_chrome.get('profile_directory', "Default"),
        browser_use_chrome_persist_user_data_dir=browser_use_chrome.get(
            'persist_user_data_dir',
            "~/.cache/method-interchangeability/browser-use-user-data-dir-persistent",
        ),
        deduplicate=capture.get('deduplicate', True),
        blacklisted_domains=capture.get('blacklisted_domains', []),
        blacklisted_url_patterns=capture.get('blacklisted_url_patterns', []),
        dedup_bypass_patterns=capture.get('dedup_bypass_patterns', []),
        max_posts=capture.get('max_posts', 100),
        output_directory=output.get('directory', 'output'),
        log_level=logging.get('level', 'INFO'),
        error_log_dir=logging.get('error_log_dir', 'logs'),
        similarity_threshold=detection.get('similarity_threshold', 95.0),
        array_style=detection.get('array_style', 'php'),
        max_json_depth=detection.get('max_json_depth', 2),
        request_timeout=detection.get('request_timeout', 30),
        rate_limit_delay=detection.get('rate_limit_delay', 1.0),
        enable_normalization=detection.get('enable_normalization', False),
        inter_site_delay=batch.get('inter_site_delay', 5.0),
        batch_mode=batch.get('mode', 'full'),
        browser_restart=batch.get('browser_restart', True),
        site_timeout=batch.get('site_timeout', 900),
        max_clickable=crawling.get('max_clickable', 10),
        max_forms=crawling.get('max_forms', 5),
        max_form_fields=crawling.get('max_form_fields', 20),
        form_action_timeout=crawling.get('form_action_timeout', 1000),
        form_interaction_timeout=crawling.get('form_interaction_timeout', 15000),
        dangerous_action_patterns=crawling.get('dangerous_action_patterns', [
            "logout", "log out", "sign out", "delete", "remove",
            "cancel", "unsubscribe", "deactivate",
            "transfer", "payment", "admin", "destroy", "password-reset"
        ]),
        agent_model=resolved_agent_model,
        agent_num_ctx=agent_ollama_num_ctx,
        agent_max_steps=agent.get('max_steps', 50),
        agent_think=agent_ollama_think,
        agent_use_vision=agent.get('use_vision', False),
        agent_provider=agent_provider,
        agent_ollama_model=agent_ollama_model,
        agent_ollama_num_ctx=agent_ollama_num_ctx,
        agent_ollama_think=agent_ollama_think,
        agent_copilot_model=agent_copilot_model,
        agent_google_model=agent_google_model,
        agent_openrouter_model=agent_openrouter_model,
        agent_openrouter_base_url=agent_openrouter_base_url,
        agent_openrouter_api_key=agent_openrouter_api_key,
        agent_vertex_model=agent_vertex_model,
        agent_vertex_project=agent_vertex_project,
        agent_vertex_location=agent_vertex_location,
        agent_azure_base_url=agent_azure_base_url,
        agent_azure_deployment=agent_azure_deployment,
        agent_azure_api_version=agent_azure_api_version,
        agent_azure_api_key=agent_azure_api_key,
    )

    # Validate configuration before returning
    _validate_config(config)

    return config


def _validate_config(config: Config) -> None:
    """
    Validate configuration values and raise clear errors for invalid settings.

    Args:
        config: Config instance to validate

    Raises:
        ValueError: If any configuration value is invalid
    """
    # Crawling validation
    if config.max_pages < 1:
        raise ValueError(f"max_pages must be >= 1, got {config.max_pages}")
    if config.max_depth < 0:
        raise ValueError(f"max_depth must be >= 0, got {config.max_depth}")
    if config.page_load_timeout <= 0:
        raise ValueError(f"page_load_timeout must be > 0, got {config.page_load_timeout}")

    # Detection validation
    if not 0 <= config.similarity_threshold <= 100:
        raise ValueError(f"similarity_threshold must be 0-100, got {config.similarity_threshold}")
    if config.array_style not in ('php', 'numbered', 'repeated'):
        raise ValueError(f"array_style must be php/numbered/repeated, got {config.array_style}")
    if config.max_json_depth < 0:
        raise ValueError(f"max_json_depth must be >= 0, got {config.max_json_depth}")
    if config.request_timeout <= 0:
        raise ValueError(f"request_timeout must be > 0, got {config.request_timeout}")
    if config.rate_limit_delay < 0:
        raise ValueError(f"rate_limit_delay must be >= 0, got {config.rate_limit_delay}")

    # Capture validation
    if config.max_posts < 0:
        raise ValueError(f"max_posts must be >= 0, got {config.max_posts}")

    # Form interaction validation
    if config.max_clickable < 0:
        raise ValueError(f"max_clickable must be >= 0, got {config.max_clickable}")
    if config.max_forms < 0:
        raise ValueError(f"max_forms must be >= 0, got {config.max_forms}")
    if config.max_form_fields < 0:
        raise ValueError(f"max_form_fields must be >= 0, got {config.max_form_fields}")
    if config.form_action_timeout <= 0:
        raise ValueError(f"form_action_timeout must be > 0, got {config.form_action_timeout}")
    if config.form_interaction_timeout <= 0:
        raise ValueError(f"form_interaction_timeout must be > 0, got {config.form_interaction_timeout}")

    # Batch validation
    if config.inter_site_delay < 0:
        raise ValueError(f"inter_site_delay must be >= 0, got {config.inter_site_delay}")
    if config.site_timeout <= 0:
        raise ValueError(f"site_timeout must be > 0, got {config.site_timeout}")
    if config.batch_mode not in ('crawl', 'detect', 'full'):
        raise ValueError(f"batch_mode must be crawl/detect/full, got {config.batch_mode}")
