"""Room-level BLE presence tracking for authenticated Sage voice devices."""
import time

from . import config, db

_TABLE = """
CREATE TABLE IF NOT EXISTS ble_presence_nodes(
    node TEXT PRIMARY KEY,
    room TEXT NOT NULL,
    rssi INTEGER,
    is_near INTEGER NOT NULL DEFAULT 0,
    last_seen REAL,
    last_report REAL NOT NULL
)
"""
_HEALTH_TABLE = """
CREATE TABLE IF NOT EXISTS ble_presence_health(
    id INTEGER PRIMARY KEY CHECK(id=1),
    coverage_since REAL
)
"""


def report(node, rssi, now=None):
    """Record a scanner heartbeat and report whether bedroom arrival qualifies for a greeting."""
    if not config.BLE_PRESENCE_ENABLED:
        raise RuntimeError("BLE presence is disabled")
    if not isinstance(node, str) or node not in config.BLE_NODE_ROOMS:
        raise ValueError("unknown BLE presence node")
    if rssi is not None and (isinstance(rssi, bool) or not isinstance(rssi, int) or not -127 <= rssi <= 0):
        raise ValueError("RSSI must be an integer from -127 to 0, or null")

    now = time.time() if now is None else float(now)
    room = config.BLE_NODE_ROOMS[node]
    near = room == "bedroom" and rssi is not None and rssi >= config.BLE_NEAR_RSSI

    with db.transaction() as conn:
        conn.execute(_TABLE)
        conn.execute(_HEALTH_TABLE)
        conn.execute("INSERT OR IGNORE INTO ble_presence_health(id,coverage_since) VALUES(1,NULL)")
        previous_near_row = conn.execute(
            "SELECT is_near FROM ble_presence_nodes WHERE node=?", (node,)
        ).fetchone()
        previous_near = bool(previous_near_row and previous_near_row["is_near"])
        last_seen_row = conn.execute(
            "SELECT MAX(last_seen) AS last_seen FROM ble_presence_nodes"
        ).fetchone()
        last_seen = last_seen_row["last_seen"]
        expected_nodes = tuple(config.BLE_NODE_ROOMS)
        previous_reports = {
            row["node"]: row["last_report"]
            for row in conn.execute(
                "SELECT node,last_report FROM ble_presence_nodes"
            ).fetchall()
        }
        was_online = bool(expected_nodes) and all(
            candidate in previous_reports and
            0 <= now - previous_reports[candidate] <= config.BLE_NODE_TIMEOUT_SECONDS
            for candidate in expected_nodes
        )

        conn.execute(
            """
            INSERT INTO ble_presence_nodes(node,room,rssi,is_near,last_seen,last_report)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(node) DO UPDATE SET
                room=excluded.room,
                rssi=excluded.rssi,
                is_near=excluded.is_near,
                last_seen=CASE WHEN excluded.last_seen IS NOT NULL
                    THEN excluded.last_seen ELSE ble_presence_nodes.last_seen END,
                last_report=excluded.last_report
            """,
            (node, room, rssi, int(near), now if rssi is not None else None, now),
        )

        if expected_nodes:
            placeholders = ",".join("?" for _ in expected_nodes)
            rows = conn.execute(
                f"SELECT node,last_report FROM ble_presence_nodes WHERE node IN ({placeholders})",
                expected_nodes,
            ).fetchall()
        else:
            rows = []
        nodes_online = len(rows) == len(expected_nodes) and all(
            0 <= now - row["last_report"] <= config.BLE_NODE_TIMEOUT_SECONDS for row in rows
        )
        health_row = conn.execute(
            "SELECT coverage_since FROM ble_presence_health WHERE id=1"
        ).fetchone()
        coverage_since = health_row["coverage_since"]
        if not nodes_online:
            coverage_since = None
        elif not was_online or coverage_since is None:
            coverage_since = now
        conn.execute(
            "UPDATE ble_presence_health SET coverage_since=? WHERE id=1",
            (coverage_since,),
        )
        away_since = max(last_seen, coverage_since) if last_seen is not None and coverage_since is not None else None
        away_long_enough = away_since is not None and now - away_since >= config.BLE_AWAY_SECONDS
        greet = bool(near and not previous_near and nodes_online and away_long_enough)

    return {
        "accepted": True,
        "room": room,
        "near": near,
        "nodes_online": nodes_online,
        "away_seconds": max(0, int(now - away_since)) if away_since is not None else None,
        "greet": greet,
    }


def where(now=None):
    """Which room the beacon is in right now: the receiver with the strongest signal above SAGE_BLE_NEAR_RSSI.
    state is 'room' (room is set), 'away' (every receiver is online but none can see the beacon), or 'unknown'
    (presence is off or a receiver is offline, so not seeing the beacon proves nothing)."""
    if not config.BLE_PRESENCE_ENABLED:
        return {"state": "unknown", "room": None}
    nodes = status(now)
    best = None
    for name, node in nodes.items():
        if not node["online"] or node["rssi"] is None or node["rssi"] < config.BLE_NEAR_RSSI:
            continue
        if best is None or node["rssi"] > best["rssi"]:
            best = {**node, "node": name}
    if best:
        return {"state": "room", "room": best["room"], "rssi": best["rssi"], "node": best["node"]}
    everyone_online = bool(nodes) and all(node["online"] for node in nodes.values())
    return {"state": "away" if everyone_online else "unknown", "room": None}


def status(now=None):
    """Return configured scanner health and latest beacon sightings."""
    now = time.time() if now is None else float(now)
    with db.transaction() as conn:
        conn.execute(_TABLE)
        conn.execute(_HEALTH_TABLE)
        conn.execute("INSERT OR IGNORE INTO ble_presence_health(id,coverage_since) VALUES(1,NULL)")
        rows = {
            row["node"]: row
            for row in conn.execute(
                "SELECT node,room,rssi,is_near,last_seen,last_report FROM ble_presence_nodes"
            ).fetchall()
        }
    return {
        node: {
            "room": room,
            "online": node in rows and 0 <= now - rows[node]["last_report"] <= config.BLE_NODE_TIMEOUT_SECONDS,
            "rssi": rows[node]["rssi"] if node in rows else None,
            "near": bool(rows[node]["is_near"]) if node in rows else False,
            "last_seen": rows[node]["last_seen"] if node in rows else None,
        }
        for node, room in config.BLE_NODE_ROOMS.items()
    }
