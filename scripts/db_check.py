import asyncio
import os
import sys
from pathlib import Path


def load_env() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key, val)


async def main() -> None:
    load_env()
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    import mini_app.app as app

    await app.init_db_pool()
    ok = await app.check_db_connection()
    await app.close_db_pool()
    print(f"DB_OK={ok}")


if __name__ == "__main__":
    asyncio.run(main())
