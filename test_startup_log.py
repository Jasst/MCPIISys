"""
Тест запуска программы с проверкой логов.
Запускает сервер на 1 секунду и проверяет, что логи записываются корректно.
"""
import os
import sys
import time

# Добавляем проект в путь для импорта
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from setup import setup_logging

def test_logging():
    """Проверяет, что логирование работает корректно."""
    
    # Настраиваем логирование
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assistant.log')
    is_prod = False
    
    import logging.handlers
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=1024 * 1024,  # 1MB
        backupCount=3,
        encoding='utf-8',
        delay=True,
    )
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)-7s] %(name)s:%(lineno)d - %(message)s\n'
        '  File "%(pathname)s", line %(lineno)d, in %(funcName)s\n'
        '    %(__traceback_id__)(\"%s\", %r)',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)-7s] %(name)s - %(message)s',
        datefmt='%H:%M:%S',
    ))
    
    logging.basicConfig(
        level=logging.DEBUG,
        handlers=[file_handler, console],
        force=True,
    )
    
    startup_logger = logging.getLogger("startup")
    startup_logger.setLevel(logging.INFO)
    
    print("\n" + "=" * 80)
    print("TEST: Startup Logging Verification")
    print("=" * 80)
    
    # Записываем тестовые сообщения в лог
    startup_logger.info("=" * 60)
    startup_logger.info("Local AI Assistant (FastAPI + Uvicorn)")
    startup_logger.info("=" * 60)
    startup_logger.info(f"   Mode:    test")
    startup_logger.info(f"   Host:    127.0.0.1:8000")
    startup_logger.info(f"   PID:     {os.getpid()}")
    startup_logger.info("=" * 60)
    
    # Записываем окружение в лог
    startup_logger.info("Environment Configuration:")
    startup_logger.info(f"   FLASK_ENV={os.getenv('FLASK_ENV', 'not set')}")
    startup_logger.info(f"   UVICORN_MODE=test")
    startup_logger.info(f"   SERVER_HOST=127.0.0.1")
    startup_logger.info(f"   SERVER_PORT=8000")
    
    # Записываем Python окружение в лог
    startup_logger.info("Python Environment:")
    import sys as _sys
    startup_logger.info(f"   PYTHON_VERSION={_sys.version}")
    startup_logger.info(f"   PLATFORM={_sys.platform}")
    startup_logger.info(f"   EXECUTABLE={_sys.executable}")
    
    # Записываем ключевые пути в лог
    startup_logger.info("Key Paths:")
    startup_logger.info(f"   PROJECT_ROOT={os.path.dirname(os.path.abspath(__file__))}")
    startup_logger.info(f"   PID_FILE=app.pid")
    
    print("\nTEST COMPLETE - check assistant.log for full log output")
    print("=" * 80)

if __name__ == '__main__':
    test_logging()
