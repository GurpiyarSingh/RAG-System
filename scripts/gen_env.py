"""Create .env from .env.example with freshly generated secrets.

Every ``change-me-*`` placeholder becomes a random value; everything else (including
the OpenAI key placeholder) is copied unchanged. Refuses to overwrite an existing
.env, because that would silently change DB passwords the database already uses.

    python scripts/gen_env.py
"""

from __future__ import annotations

import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    target = ROOT / ".env"
    if target.exists():
        sys.exit(".env already exists; edit it by hand (or delete it first).")
    text = (ROOT / ".env.example").read_text(encoding="utf-8")

    generated: dict[str, str] = {}

    def secret_for(match: re.Match[str]) -> str:
        name = match.group(1)
        # URL-safe and shell-safe: no characters that need escaping in DSNs or .env.
        generated.setdefault(name, secrets.token_urlsafe(24).replace("-", "x").replace("_", "y"))
        return generated[name]

    text = re.sub(r"change-me-([a-z-]+)", secret_for, text)
    # The admin key gets a recognizable prefix so it's obvious in logs and configs.
    text = text.replace(
        f"RAG_BOOTSTRAP_ADMIN_KEY={generated['admin-key']}",
        f"RAG_BOOTSTRAP_ADMIN_KEY=rag_admin_{generated['admin-key']}",
    )
    target.write_text(text, encoding="utf-8")
    print("Created .env with random secrets. Add your OpenAI key to OPENAI_API_KEY when ready.")


if __name__ == "__main__":
    main()
