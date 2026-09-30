"""
T1: 配置加载模块
从 .env 文件读取环境变量，缺少必要配置时抛出明确错误。
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录
_ROOT_DIR = Path(__file__).parent.parent.parent

# 自动加载 .env（存在时）
load_dotenv(dotenv_path=_ROOT_DIR / ".env", override=False)

# Kimi（Moonshot）OpenAI 兼容接口默认地址
_DEFAULT_KIMI_BASE_URL = "https://api.moonshot.cn/v1"

# .env.example 中的占位符，视为「未配置」以便回退到 OPENAI_API_KEY 等
_KIMI_KEY_PLACEHOLDERS = frozenset(
    {
        "",
        "your_moonshot_api_key_here",
    }
)


def _kimi_api_key_from_env() -> str:
    """优先 MOONSHOT_API_KEY；若为占位或未设，则使用 OPENAI_API_KEY（兼容常见 OpenAI 客户端命名）。"""
    moonshot = os.environ.get("MOONSHOT_API_KEY", "").strip()
    openai_compat = os.environ.get("OPENAI_API_KEY", "").strip()
    if moonshot and moonshot not in _KIMI_KEY_PLACEHOLDERS:
        return moonshot
    if openai_compat and openai_compat not in _KIMI_KEY_PLACEHOLDERS:
        return openai_compat
    return ""


def _kimi_model_from_env() -> str:
    for name in ("MOONSHOT_MODEL", "OPENAI_MODEL"):
        v = os.environ.get(name, "").strip()
        if v:
            return v
    return "kimi-k2.5"


def _kimi_base_url_from_env() -> str:
    for name in ("LLM_BASE_URL", "MOONSHOT_BASE_URL"):
        v = os.environ.get(name, "").strip()
        if v:
            return v
    return _DEFAULT_KIMI_BASE_URL


def _resolve_whisper_model_path() -> str:
    """
    WHISPER_MODEL_PATH：指向本地 faster-whisper（CTranslate2）模型目录，或留空。
    相对路径按「用户主目录」解析，例如 Documents/00-models/faster-whisper-large-v3。
    不能指向 OpenAI 的 .pt 权重文件；faster-whisper 与 .pt 格式不兼容。
    """
    raw = os.environ.get("WHISPER_MODEL_PATH", "").strip()
    if not raw:
        return ""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path.home() / raw
    return str(p.resolve())


@dataclass
class Config:
    """运行配置。默认使用 Kimi（Moonshot）；可切换为 Anthropic Claude。"""

    llm_provider: str  # "kimi" | "anthropic"
    llm_api_key: str
    llm_model: str
    llm_base_url: str = ""
    output_dir: str = field(default_factory=lambda: str(_ROOT_DIR / "04-output"))
    log_dir: str = field(default_factory=lambda: str(_ROOT_DIR / "05-log"))
    whisper_model: str = "large-v3"
    whisper_model_path: str = ""
    groq_api_key: str = ""
    mp3_subdir: str = "01-mp3s"
    response_jsons_subdir: str = "02-response"   # Whisper API 输出物（json/txt），按剧集子目录
    para_jsons_subdir: str = "03-paraJsons"      # 管道中间 JSON 缓存（Step 4 内容提取），按剧集子目录
    htmls_subdir: str = "04-htmls"               # 三场景 HTML + 知识图 PNG，按剧集子目录
    date_format: str = "%Y%m%d"
    retry_delay: float = 3.0


def get_config() -> Config:
    """
    加载并返回配置对象。
    默认 LLM_PROVIDER=kimi，需设置 MOONSHOT_API_KEY 或 OPENAI_API_KEY；
    可选 LLM_BASE_URL / OPENAI_MODEL（与 MOONSHOT_* 等价语义）；
    LLM_PROVIDER=anthropic 时需设置 ANTHROPIC_API_KEY。
    """
    from utils.exceptions import EnvironmentError as PodcastEnvError

    provider = os.environ.get("LLM_PROVIDER", "kimi").strip().lower()
    if provider not in ("kimi", "anthropic"):
        raise PodcastEnvError(
            'LLM_PROVIDER 必须是 "kimi" 或 "anthropic"。'
        )

    if provider == "kimi":
        api_key = _kimi_api_key_from_env()
        if not api_key:
            raise PodcastEnvError(
                "缺少 MOONSHOT_API_KEY 或 OPENAI_API_KEY，请在 .env 或环境变量中设置其一。"
                "（Kimi 开放平台：https://platform.moonshot.cn ）"
            )
        model = _kimi_model_from_env()
        base_url = _kimi_base_url_from_env()
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise PodcastEnvError(
                "缺少 ANTHROPIC_API_KEY，请在 .env 文件或环境变量中设置。"
                "（使用 Claude 时请设置 LLM_PROVIDER=anthropic）"
            )
        model = os.environ.get(
            "CLAUDE_MODEL", "claude-3-5-sonnet-20241022"
        ).strip() or "claude-3-5-sonnet-20241022"
        base_url = ""

    return Config(
        llm_provider=provider,
        llm_api_key=api_key,
        llm_model=model,
        llm_base_url=base_url,
        output_dir=os.environ.get("OUTPUT_DIR", str(_ROOT_DIR / "04-output")),
        log_dir=os.environ.get("LOG_DIR", str(_ROOT_DIR / "05-log")),
        whisper_model=os.environ.get("WHISPER_MODEL", "large-v3"),
        whisper_model_path=_resolve_whisper_model_path(),
        groq_api_key=os.environ.get("GROQ_API_KEY", "").strip(),
        mp3_subdir=os.environ.get("MP3_SUBDIR", "01-mp3s"),
        response_jsons_subdir=os.environ.get("RESPONSE_SUBDIR", "02-response"),
        para_jsons_subdir=os.environ.get("PARA_JSONS_SUBDIR", "03-paraJsons"),
        htmls_subdir=os.environ.get("HTMLS_SUBDIR", "04-htmls"),
        date_format=os.environ.get("DATE_FORMAT", "%Y%m%d"),
    )
