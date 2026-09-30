"""
T2: 统一日志模块
- 终端输出 INFO 级别（简要信息）
- 文件记录 DEBUG 级别（详细日志）
- 日志文件按日期命名：log/YYYY-MM-DD.log
- 格式：时间戳 | 级别 | 模块名 | 消息
"""
import logging
import datetime
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 全局根 logger 名称
_ROOT_LOGGER_NAME = "podcastmap"
_NAME_PREFIX = f"{_ROOT_LOGGER_NAME}."


class _PodcastMapFormatter(logging.Formatter):
    """输出时去掉 logger 名中的 podcastmap. 前缀。"""

    def format(self, record: logging.LogRecord) -> str:
        name = record.name
        if name.startswith(_NAME_PREFIX):
            record.name = name[len(_NAME_PREFIX):]
        elif name == _ROOT_LOGGER_NAME:
            record.name = "root"
        return super().format(record)


def setup_logger(
    log_dir: str = "05-log",
    name: str = _ROOT_LOGGER_NAME,
    stream_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
) -> logging.Logger:
    """
    初始化并返回项目 logger。
    可多次调用（重复调用时不会添加重复 handler）。
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    today = datetime.date.today().strftime("%Y-%m-%d")
    log_file = log_path / f"{today}.log"

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)  # 根级别最低，由 handler 过滤

    # 避免重复添加 handler（pytest 多次调用场景）
    if logger.handlers:
        logger.handlers.clear()

    formatter = _PodcastMapFormatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # 终端 handler（INFO）
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(stream_level)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    # 文件 handler（DEBUG）
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(file_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # 防止日志向上传播到 root logger（避免重复输出）
    logger.propagate = False

    return logger


def get_logger(module_name: str) -> logging.Logger:
    """获取子模块 logger，命名格式：podcastmap.<module_name>"""
    return logging.getLogger(f"{_ROOT_LOGGER_NAME}.{module_name}")
