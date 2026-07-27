from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "fraeno.github_app.app:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
