import asyncio
import hashlib
import json
import re
import secrets
import sqlite3
from datetime import datetime, timezone

HOST = "0.0.0.0"
PORT = 8765
DB = "data/connect.db"
PRESENCE_PING_INTERVAL = 20
PRESENCE_TIMEOUT = 50

online = {}
last_seen = {}


def now():
    return datetime.now(timezone.utc).isoformat()


def database():
    db = sqlite3.connect(DB)
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            delivered INTEGER DEFAULT 0
        )
    """)

    columns = {
        row[1] for row in db.execute("PRAGMA table_info(messages)")
    }
    if "read_at" not in columns:
        db.execute("ALTER TABLE messages ADD COLUMN read_at TEXT")

    db.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS group_members (
            group_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',
            joined_at TEXT NOT NULL,
            PRIMARY KEY (group_id, username)
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS group_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            sender TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_group_members_user
        ON group_members(username, group_id)
    """)

    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_group_messages_group
        ON group_messages(group_id, id)
    """)

    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_recipient
        ON messages(recipient, id)
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_sender
        ON messages(sender, id)
    """)
    db.commit()
    return db


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), 200000
    ).hex()


def authenticate(db, request):
    username = str(request.get("username", "")).lower().strip()
    password = str(request.get("password", ""))

    if not re.fullmatch(r"[a-z0-9_]{3,24}", username):
        return False, "Username must be 3-24 characters."

    if len(password) < 8 or len(password) > 128:
        return False, "Password must contain 8-128 characters."

    row = db.execute(
        "SELECT salt, password_hash FROM users WHERE username=?",
        (username,)
    ).fetchone()

    if request.get("type") == "register":
        if row:
            return False, "Username already exists."

        salt = secrets.token_hex(16)
        password_hash = hash_password(password, salt)
        db.execute(
            "INSERT INTO users VALUES (?, ?, ?, ?)",
            (username, salt, password_hash, now())
        )
        db.commit()
        return True, username

    if request.get("type") == "login":
        if not row:
            return False, "Account does not exist."
        if not secrets.compare_digest(
            hash_password(password, row[0]), row[1]
        ):
            return False, "Incorrect password."
        return True, username

    return False, "Unknown authentication method."


async def send(writer, data):
    writer.write((json.dumps(data) + "\n").encode())
    await writer.drain()


async def broadcast_presence(username, is_online, exclude=None):
    pending = [(username, is_online)]
    first_event = True

    while pending:
        current_username, current_online = pending.pop(0)
        packet = {
            "type": "presence_change",
            "username": current_username,
            "online": current_online,
            "last_seen": last_seen.get(current_username)
        }
        skip_writer = exclude if first_event else None
        first_event = False

        for peer, peer_writer in list(online.items()):
            if peer_writer is skip_writer:
                continue
            try:
                await send(peer_writer, packet)
            except (ConnectionError, OSError):
                if online.get(peer) is peer_writer:
                    del online[peer]
                    seen_at = now()
                    last_seen[peer] = seen_at
                    pending.append((peer, False))


async def mark_offline(username, writer):
    if online.get(username) is not writer:
        return
    del online[username]
    last_seen[username] = now()
    print(f"[OFFLINE] @{username}")
    await broadcast_presence(username, False)


async def monitor_presence(username, writer, last_pong):
    loop = asyncio.get_running_loop()
    while online.get(username) is writer:
        await asyncio.sleep(PRESENCE_PING_INTERVAL)
        if online.get(username) is not writer:
            return
        if loop.time() - last_pong[0] > PRESENCE_TIMEOUT:
            await mark_offline(username, writer)
            writer.close()
            return
        try:
            await send(writer, {"type": "presence_ping"})
        except (ConnectionError, OSError):
            await mark_offline(username, writer)
            writer.close()
            return


async def deliver_pending(db, username, writer):
    rows = db.execute("""
        SELECT id, sender, body, created_at
        FROM messages
        WHERE recipient=? AND delivered=0
        ORDER BY id
    """, (username,)).fetchall()

    delivered_ids = []

    for msg_id, sender, body, created in rows:
        await send(writer, {
            "type": "message",
            "id": msg_id,
            "from": sender,
            "text": body,
            "time": created,
            "offline": True
        })
        db.execute(
            "UPDATE messages SET delivered=1 WHERE id=?", (msg_id,)
        )
        delivered_ids.append((msg_id, sender))

    db.commit()

    for msg_id, sender in delivered_ids:
        sender_writer = online.get(sender)
        if sender_writer:
            try:
                await send(sender_writer, {
                    "type": "delivered",
                    "id": msg_id
                })
            except (ConnectionError, OSError):
                if online.get(sender) is sender_writer:
                    await mark_offline(sender, sender_writer)


async def send_inbox(db, username, writer):
    rows = db.execute("""
        SELECT
            c.other,
            m.body,
            m.sender,
            m.created_at,
            (
                SELECT COUNT(*)
                FROM messages unread
                WHERE unread.sender=c.other
                  AND unread.recipient=?
                  AND unread.read_at IS NULL
            ) AS unread_count
        FROM (
            SELECT
                CASE
                    WHEN sender=? THEN recipient
                    ELSE sender
                END AS other,
                MAX(id) AS last_id
            FROM messages
            WHERE sender=? OR recipient=?
            GROUP BY other
        ) c
        JOIN messages m ON m.id=c.last_id
        ORDER BY m.id DESC
    """, (username, username, username, username)).fetchall()

    conversations = []
    for other, body, sender, created, unread in rows:
        conversations.append({
            "username": other,
            "last_text": body,
            "last_sender": sender,
            "time": created,
            "unread": unread,
            "online": other in online,
            "last_seen": last_seen.get(other)
        })

    await send(writer, {
        "type": "inbox",
        "conversations": conversations,
        "online_users": sorted(online.keys()),
        "last_seen": dict(last_seen)
    })


async def handle_client(reader, writer):
    db = database()
    username = None
    heartbeat_task = None

    try:
        address = writer.get_extra_info("peername")
        print(f"[CONNECT] {address}")

        raw = await asyncio.wait_for(reader.readline(), timeout=60)
        if not raw:
            return

        request = json.loads(raw.decode())
        success, result = authenticate(db, request)

        if not success:
            await send(writer, {"type": "error", "message": result})
            return

        username = result

        if username in online:
            await send(writer, {
                "type": "error",
                "message": "This account is already online."
            })
            return

        online[username] = writer

        await send(writer, {
            "type": "welcome",
            "username": username,
            "server": "JAYMOH CONNECT",
            "version": "0.3.0"
        })

        print(f"[ONLINE] @{username} from {address}")

        await send(writer, {
            "type": "presence_snapshot",
            "online_users": sorted(online.keys()),
            "last_seen": dict(last_seen)
        })
        await broadcast_presence(username, True, exclude=writer)
        await deliver_pending(db, username, writer)
        last_pong = [asyncio.get_running_loop().time()]
        heartbeat_task = asyncio.create_task(
            monitor_presence(username, writer, last_pong)
        )

        while True:
            raw = await reader.readline()
            if not raw:
                break

            request = json.loads(raw.decode())
            kind = request.get("type")

            if kind == "presence_pong":
                last_pong[0] = asyncio.get_running_loop().time()
                continue

            if kind == "message":
                recipient = str(request.get("to", "")).lower().strip()
                body = str(request.get("text", "")).strip()

                if not re.fullmatch(r"[a-z0-9_]{3,24}", recipient):
                    await send(writer, {
                        "type": "error", "message": "Invalid recipient."
                    })
                    continue

                if not body or len(body) > 4000:
                    await send(writer, {
                        "type": "error",
                        "message": "Message must contain 1-4000 characters."
                    })
                    continue

                exists = db.execute(
                    "SELECT 1 FROM users WHERE username=?", (recipient,)
                ).fetchone()

                if not exists:
                    await send(writer, {
                        "type": "error",
                        "message": "That user does not exist."
                    })
                    continue

                created = now()
                cursor = db.execute("""
                    INSERT INTO messages
                    (sender, recipient, body, created_at, delivered)
                    VALUES (?, ?, ?, ?, 0)
                """, (username, recipient, body, created))
                db.commit()
                msg_id = cursor.lastrowid

                target = online.get(recipient)
                delivered_now = False

                if target:
                    try:
                        await send(target, {
                            "type": "message",
                            "id": msg_id,
                            "from": username,
                            "text": body,
                            "time": created,
                            "offline": False
                        })
                        db.execute(
                            "UPDATE messages SET delivered=1 WHERE id=?",
                            (msg_id,)
                        )
                        db.commit()
                        delivered_now = True
                    except (ConnectionError, OSError):
                        if online.get(recipient) is target:
                            await mark_offline(recipient, target)

                await send(writer, {
                    "type": "sent",
                    "id": msg_id,
                    "to": recipient,
                    "time": created
                })

                if delivered_now:
                    await send(writer, {
                        "type": "delivered",
                        "id": msg_id
                    })

            elif kind == "group_create":
                name = str(request.get("name", "")).strip()

                if not 2 <= len(name) <= 40:
                    await send(writer, {
                        "type": "error",
                        "message": "Group name must contain 2-40 characters."
                    })
                    continue

                created = now()
                cursor = db.execute("""
                    INSERT INTO groups (name, created_by, created_at)
                    VALUES (?, ?, ?)
                """, (name, username, created))

                group_id = cursor.lastrowid

                db.execute("""
                    INSERT INTO group_members
                    (group_id, username, role, joined_at)
                    VALUES (?, ?, 'admin', ?)
                """, (group_id, username, created))

                db.commit()

                await send(writer, {
                    "type": "group_created",
                    "group_id": group_id,
                    "name": name
                })

            elif kind == "groups":
                rows = db.execute("""
                    SELECT
                        g.id,
                        g.name,
                        g.created_by,
                        g.created_at,
                        (
                            SELECT COUNT(*)
                            FROM group_members gm
                            WHERE gm.group_id=g.id
                        ),
                        (
                            SELECT body
                            FROM group_messages msg
                            WHERE msg.group_id=g.id
                            ORDER BY msg.id DESC
                            LIMIT 1
                        )
                    FROM groups g
                    JOIN group_members mine ON mine.group_id=g.id
                    WHERE mine.username=?
                    ORDER BY g.id DESC
                """, (username,)).fetchall()

                await send(writer, {
                    "type": "groups",
                    "groups": [
                        {
                            "id": row[0],
                            "name": row[1],
                            "created_by": row[2],
                            "created_at": row[3],
                            "member_count": row[4],
                            "last_message": row[5] or ""
                        }
                        for row in rows
                    ]
                })

            elif kind == "group_add":
                try:
                    group_id = int(request.get("group_id"))
                except (TypeError, ValueError):
                    await send(writer, {
                        "type": "error", "message": "Invalid group."
                    })
                    continue

                member = str(request.get("username", "")).lower().strip()

                if not re.fullmatch(r"[a-z0-9_]{3,24}", member):
                    await send(writer, {
                        "type": "error", "message": "Invalid username."
                    })
                    continue

                admin = db.execute("""
                    SELECT 1 FROM group_members
                    WHERE group_id=? AND username=? AND role='admin'
                """, (group_id, username)).fetchone()

                if not admin:
                    await send(writer, {
                        "type": "error",
                        "message": "Only group admins can add members."
                    })
                    continue

                exists = db.execute(
                    "SELECT 1 FROM users WHERE username=?", (member,)
                ).fetchone()

                if not exists:
                    await send(writer, {
                        "type": "error",
                        "message": "That user does not exist."
                    })
                    continue

                already = db.execute("""
                    SELECT 1 FROM group_members
                    WHERE group_id=? AND username=?
                """, (group_id, member)).fetchone()

                if already:
                    await send(writer, {
                        "type": "error",
                        "message": "That user is already a member."
                    })
                    continue

                db.execute("""
                    INSERT INTO group_members
                    (group_id, username, role, joined_at)
                    VALUES (?, ?, 'member', ?)
                """, (group_id, member, now()))
                db.commit()

                await send(writer, {
                    "type": "group_member_added",
                    "group_id": group_id,
                    "username": member
                })

                member_writer = online.get(member)
                if member_writer:
                    try:
                        await send(member_writer, {
                            "type": "group_invited",
                            "group_id": group_id
                        })
                    except (ConnectionError, OSError):
                        if online.get(member) is member_writer:
                            await mark_offline(member, member_writer)

            elif kind == "group_remove":
                try:
                    group_id = int(request.get("group_id"))
                except (TypeError, ValueError):
                    await send(writer, {
                        "type": "error", "message": "Invalid group."
                    })
                    continue

                member = str(request.get("username", "")).lower().strip().lstrip("@")
                if not re.fullmatch(r"[a-z0-9_]{3,24}", member):
                    await send(writer, {
                        "type": "error", "message": "Invalid username."
                    })
                    continue

                admin = db.execute("""
                    SELECT 1 FROM group_members
                    WHERE group_id=? AND username=? AND role='admin'
                """, (group_id, username)).fetchone()
                if not admin:
                    await send(writer, {
                        "type": "error",
                        "message": "Only group admins can remove members."
                    })
                    continue

                target = db.execute("""
                    SELECT role FROM group_members
                    WHERE group_id=? AND username=?
                """, (group_id, member)).fetchone()
                if not target:
                    await send(writer, {
                        "type": "error",
                        "message": "That user is not a member of this group."
                    })
                    continue
                if target[0] == "admin":
                    await send(writer, {
                        "type": "error",
                        "message": "Group admins cannot be removed."
                    })
                    continue

                db.execute("""
                    DELETE FROM group_members
                    WHERE group_id=? AND username=? AND role='member'
                """, (group_id, member))
                db.commit()

                member_writer = online.get(member)
                if member_writer:
                    try:
                        await send(member_writer, {
                            "type": "group_member_removed_notice",
                            "group_id": group_id
                        })
                    except (ConnectionError, OSError):
                        if online.get(member) is member_writer:
                            await mark_offline(member, member_writer)

                await send(writer, {
                    "type": "group_member_removed",
                    "group_id": group_id,
                    "username": member
                })

            elif kind == "group_members":
                try:
                    group_id = int(request.get("group_id"))
                except (TypeError, ValueError):
                    await send(writer, {
                        "type": "error", "message": "Invalid group."
                    })
                    continue

                membership = db.execute("""
                    SELECT 1 FROM group_members
                    WHERE group_id=? AND username=?
                """, (group_id, username)).fetchone()

                if not membership:
                    await send(writer, {
                        "type": "error",
                        "message": "You are not a member of this group."
                    })
                    continue

                rows = db.execute("""
                    SELECT username, role, joined_at
                    FROM group_members
                    WHERE group_id=?
                    ORDER BY CASE WHEN role='admin' THEN 0 ELSE 1 END,
                             username COLLATE NOCASE
                """, (group_id,)).fetchall()

                await send(writer, {
                    "type": "group_members",
                    "group_id": group_id,
                    "members": [
                        {
                            "username": row[0],
                            "role": row[1],
                            "joined_at": row[2],
                            "online": row[0] in online,
                            "last_seen": last_seen.get(row[0])
                        }
                        for row in rows
                    ]
                })

            elif kind == "group_message":
                try:
                    group_id = int(request.get("group_id"))
                except (TypeError, ValueError):
                    await send(writer, {
                        "type": "error", "message": "Invalid group."
                    })
                    continue

                body = str(request.get("text", "")).strip()

                if not body or len(body) > 4000:
                    await send(writer, {
                        "type": "error",
                        "message": "Message must contain 1-4000 characters."
                    })
                    continue

                membership = db.execute("""
                    SELECT 1 FROM group_members
                    WHERE group_id=? AND username=?
                """, (group_id, username)).fetchone()

                if not membership:
                    await send(writer, {
                        "type": "error",
                        "message": "You are not a member of this group."
                    })
                    continue

                created = now()
                cursor = db.execute("""
                    INSERT INTO group_messages
                    (group_id, sender, body, created_at)
                    VALUES (?, ?, ?, ?)
                """, (group_id, username, body, created))
                db.commit()

                msg_id = cursor.lastrowid

                members = db.execute("""
                    SELECT username FROM group_members
                    WHERE group_id=?
                """, (group_id,)).fetchall()

                packet = {
                    "type": "group_message",
                    "id": msg_id,
                    "group_id": group_id,
                    "from": username,
                    "text": body,
                    "time": created
                }

                for (member_name,) in members:
                    if member_name == username:
                        continue

                    member_writer = online.get(member_name)
                    if member_writer:
                        try:
                            await send(member_writer, packet)
                        except (ConnectionError, OSError):
                            if online.get(member_name) is member_writer:
                                await mark_offline(member_name, member_writer)

                await send(writer, {
                    "type": "group_sent",
                    "id": msg_id,
                    "group_id": group_id,
                    "time": created
                })

            elif kind == "group_history":
                try:
                    group_id = int(request.get("group_id"))
                except (TypeError, ValueError):
                    await send(writer, {
                        "type": "error", "message": "Invalid group."
                    })
                    continue

                membership = db.execute("""
                    SELECT 1 FROM group_members
                    WHERE group_id=? AND username=?
                """, (group_id, username)).fetchone()

                if not membership:
                    await send(writer, {
                        "type": "error",
                        "message": "You are not a member of this group."
                    })
                    continue

                rows = db.execute("""
                    SELECT id, sender, body, created_at
                    FROM group_messages
                    WHERE group_id=?
                    ORDER BY id DESC
                    LIMIT 100
                """, (group_id,)).fetchall()

                await send(writer, {
                    "type": "group_history",
                    "group_id": group_id,
                    "messages": [
                        {
                            "id": row[0],
                            "from": row[1],
                            "text": row[2],
                            "time": row[3]
                        }
                        for row in reversed(rows)
                    ]
                })

            elif kind == "inbox":
                await send_inbox(db, username, writer)

            elif kind == "users":
                await send(writer, {
                    "type": "users",
                    "users": sorted(online.keys())
                })

            elif kind == "history":
                other = str(request.get("with", "")).lower().strip()

                if not re.fullmatch(r"[a-z0-9_]{3,24}", other):
                    await send(writer, {
                        "type": "error", "message": "Invalid username."
                    })
                    continue

                exists = db.execute(
                    "SELECT 1 FROM users WHERE username=?", (other,)
                ).fetchone()
                if not exists:
                    await send(writer, {
                        "type": "error", "message": "User does not exist."
                    })
                    continue

                unread_rows = db.execute("""
                    SELECT id FROM messages
                    WHERE sender=? AND recipient=? AND read_at IS NULL
                """, (other, username)).fetchall()

                db.execute("""
                    UPDATE messages
                    SET read_at=?
                    WHERE sender=? AND recipient=? AND read_at IS NULL
                """, (now(), other, username))
                db.commit()

                sender_writer = online.get(other)
                if sender_writer:
                    for (read_id,) in unread_rows:
                        try:
                            await send(sender_writer, {
                                "type": "read",
                                "id": read_id
                            })
                        except (ConnectionError, OSError):
                            if online.get(other) is sender_writer:
                                await mark_offline(other, sender_writer)
                            break

                rows = db.execute("""
                    SELECT id, sender, recipient, body, created_at,
                           delivered, read_at
                    FROM messages
                    WHERE (sender=? AND recipient=?)
                       OR (sender=? AND recipient=?)
                    ORDER BY id DESC LIMIT 50
                """, (username, other, other, username)).fetchall()

                await send(writer, {
                    "type": "history",
                    "with": other,
                    "presence": {
                        "online": other in online,
                        "last_seen": last_seen.get(other)
                    },
                    "messages": [
                        {
                            "id": row[0],
                            "from": row[1],
                            "to": row[2],
                            "text": row[3],
                            "time": row[4],
                            "delivered": bool(row[5]),
                            "read_at": row[6]
                        }
                        for row in reversed(rows)
                    ]
                })

            elif kind == "quit":
                await send(writer, {"type": "goodbye"})
                break

    except (ConnectionError, asyncio.TimeoutError):
        pass
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        print(f"[INVALID DATA] {error}")
    except Exception as error:
        print(f"[ERROR] {error}")
    finally:
        if heartbeat_task:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if username and online.get(username) is writer:
            await mark_offline(username, writer)

        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        db.close()


async def main():
    server = await asyncio.start_server(
        handle_client, HOST, PORT
    )
    print("=" * 45)
    print("       JAYMOH CONNECT SERVER")
    print("       VERSION 0.3.0")
    print("=" * 45)
    print(f"[READY] {HOST}:{PORT}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[STOPPED] Server shutting down.")
