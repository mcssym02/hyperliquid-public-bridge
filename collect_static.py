from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app import health, history, snapshot, xaut

DATA_DIR = Path("data")


def write_json(name: str, payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


async def main() -> None:
    health_data, snapshot_data, history_data, xaut_data = await asyncio.gather(
        health(),
        snapshot(),
        history(2),
        xaut(),
    )

    write_json("health.json", health_data)
    write_json("snapshot.json", snapshot_data)
    write_json("history-2h.json", history_data)
    write_json("xaut.json", xaut_data)

    print(
        json.dumps(
            {
                "health": health_data.get("status"),
                "snapshot": snapshot_data.get("status"),
                "history_2h": history_data.get("status"),
                "xaut": xaut_data.get("status"),
                "timestamp_utc": health_data.get("timestamp_utc"),
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
