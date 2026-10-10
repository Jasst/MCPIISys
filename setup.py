"""
setup.py — логирование и rate limiter.
"""
import logging
import logging.handlers
import os
import threading
import time
from collections import defaultdict
from typing import Dict, Tuple

from config import CONFIG


def setup_logging() -> logging.Logger:
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assistant.log')
    is_prod = os.getenv('FLASK_ENV') == 'production'

    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=CONFIG['LOG_MAX_BYTES'],
        backupCount=CONFIG['LOG_BACKUP_COUNT'],
        encoding='utf-8',
        delay=True,
    )
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s [%(name)s:%(lineno)d] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    handlers = [file_handler]
    if not is_prod:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
        handlers.append(console)

    logging.basicConfig(
        level=logging.WARNING if is_prod else logging.INFO,
        handlers=handlers,
        force=True,
    )
    logging.getLogger('uvicorn').setLevel(logging.WARNING)
    logger = logging.getLogger(__name__)
    logger.warning(f"=== AI assistant started, log: {log_path} ===")
    return logger


class RateLimiter:
    def __init__(self, max_requests: int = 60, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._clients: Dict[str, list] = defaultdict(list)
        self._lock = threading.Lock()
        self._cleanup_counter = 0

    def is_allowed(self, client_id: str, limit: int = None) -> Tuple[bool, int]:
        max_req = limit or self.max_requests
        now = time.time()
        with self._lock:
            self._clients[client_id] = [
                t for t in self._clients[client_id] if now - t < self.window_seconds
            ]
            if len(self._clients[client_id]) >= max_req:
                return False, 0
            self._clients[client_id].append(now)
            remaining = max_req - len(self._clients[client_id])
            self._cleanup_counter += 1
            if self._cleanup_counter > 100:
                self._cleanup()
                self._cleanup_counter = 0
            return True, remaining

    def _cleanup(self):
        now = time.time()
        expired = [
            cid for cid, ts in self._clients.items()
            if not ts or (now - ts[-1]) > self.window_seconds * 2
        ]
        for cid in expired:
            del self._clients[cid]

    def get_stats(self) -> dict:
        with self._lock:
            return {
                'total_clients': len(self._clients),
                'active_clients': sum(
                    1 for ts in self._clients.values()
                    if ts and time.time() - ts[-1] < self.window_seconds
                ),
                'window_seconds': self.window_seconds,
                'max_requests': self.max_requests,
            }


general_limiter = RateLimiter(max_requests=CONFIG['RATE_LIMIT_PER_MINUTE'])
message_limiter = RateLimiter(max_requests=CONFIG['RATE_LIMIT_MESSAGE_PER_MINUTE'])


def get_rate_limit_stats() -> dict:
    return {
        'general': general_limiter.get_stats(),
        'messages': message_limiter.get_stats(),
    }
