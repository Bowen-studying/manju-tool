"""Shared configuration utilities — env loading from os.environ and ~/.manju.env.

Used by ai.py and all generate_*.py modules to avoid duplicating the same
credential-loading logic across the codebase.
"""

import os
import sys


def load_manju_env() -> dict:
    """Load environment variables from os.environ and ~/.manju.env.

    os.environ takes priority over ~/.manju.env values.
    Results are NOT cached — each call re-reads the env file.

    Returns a dict of all resolved env vars.
    """
    env_keys = dict(os.environ)

    env_file = os.path.join(os.path.expanduser("~"), ".manju.env")
    try:
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    key = key.strip()
                    if key not in env_keys:
                        env_keys[key] = val.strip()
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"   ⚠ 读取 ~/.manju.env 失败: {e}", file=sys.stderr)

    return env_keys


def count_chinese(text: str) -> int:
    """Count Chinese characters in a string."""
    return sum(1 for c in text if '一' <= c <= '鿿')
