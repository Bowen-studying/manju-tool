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
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    if line.startswith("export "):
                        line = line[7:].lstrip()
                    key, _, val = line.partition("=")
                    key = key.strip()
                    if key not in env_keys:
                        val = val.strip()
                        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                            val = val[1:-1]
                        env_keys[key] = val
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"   ⚠ 读取 ~/.manju.env 失败: {e}", file=sys.stderr)

    return env_keys


def count_chinese(text: str) -> int:
    """Count Chinese characters in a string."""
    return sum(1 for c in text if '一' <= c <= '鿿')


def count_content_units(text: str) -> int:
    """Estimate content length for Chinese, Latin text, and structured JSON."""
    chinese = count_chinese(text)
    non_chinese = sum(1 for c in text if not ('一' <= c <= '鿿') and not c.isspace())
    return chinese + (non_chinese + 3) // 4
