import curses
import getpass
import json
import select
import socket
import sys
import textwrap
import time
from datetime import datetime

PORT = 8765
VERSION = "0.6.0"


def send(sock, data):
    sock.sendall((json.dumps(data) + "\n").encode())


def time_label(value):
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M")
    except (ValueError, TypeError):
        return value[11:16] if len(value) >= 16 else ""


def presence_time_label(value):
    if not value:
        return "unknown"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(value).replace("T", " ")[:16]


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"

    RESET = "\033[0m"
    GREEN = "\033[38;5;114m"
    MUTED = "\033[38;5;245m"
    WHITE = "\033[38;5;255m"
    BOLD = "\033[1m"

    def show_login():
        import shutil

        width = max(50, min(shutil.get_terminal_size().columns, 100))
        inner = min(width - 8, 64)

        def center(text):
            return text.center(inner)

        print("\033[2J\033[H", end="")
        print()
        print(GREEN + BOLD + center("J A Y M O H   C O N N E C T") + RESET)
        print(MUTED + center("YOUR MESSAGING SPACE") + RESET)
        print()
        print(MUTED + center("─" * (inner - 4)) + RESET)
        print()
        print(WHITE + BOLD + center("WELCOME TO CONNECT") + RESET)
        print()
        print(MUTED + center("SERVER") + RESET)
        print(WHITE + center(f"{host}:{PORT}") + RESET)
        print()
        print(GREEN + BOLD + center("YOUR CONNECTION STARTS HERE.") + RESET)
        print(MUTED + center("Sign in to continue or create an account to join Connect.") + RESET)
        print()
        print(MUTED + center("─" * (inner - 4)) + RESET)
        print()
        print(GREEN + BOLD + center("[ 1 ]") + RESET)
        print(WHITE + center("SIGN IN TO YOUR ACCOUNT") + RESET)
        print()
        print(GREEN + BOLD + center("[ 2 ]") + RESET)
        print(WHITE + center("CREATE A NEW ACCOUNT") + RESET)
        print()
        print(MUTED + center("─" * (inner - 4)) + RESET)
        print()

    show_login()

    mode = input("  Select an option  [1/2]: ").strip()

    if mode not in ("1", "2"):
        print("\n  Invalid selection. Please restart and choose 1 or 2.")
        return

    print()
    username = input("  Username: ").strip().lower()

    if not username:
        print("\n  Username cannot be empty.")
        input("\n  Press Enter to close...")
        return

    if any(char.isspace() for char in username):
        print("\n  Username cannot contain spaces.")
        input("\n  Press Enter to close...")
        return

    print()
    password = input("  Password: ")

    if not password:
        print("\n  Password cannot be empty.")
        input("\n  Press Enter to close...")
        return

    if mode == "2":
        confirmation = input("  Confirm password: ")

        if password != confirmation:
            print("\n  Passwords do not match.")
            input("\n  Press Enter to close...")
            return
        confirmation = ""

    credential = bytearray(password.encode("utf-8"))
    password = ""
    sock = None
    initial_buffer = b""

    try:
        sock = socket.create_connection((host, PORT), timeout=15)
        sock.settimeout(15)

        send(sock, {
            "type": "register" if mode == "2" else "login",
            "username": username,
            "password": credential.decode("utf-8")
        })

        raw = b""
        while b"\n" not in raw:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Server closed the connection.")
            raw += chunk

        response_line, initial_buffer = raw.split(b"\n", 1)
        response = json.loads(response_line.decode())

        if response.get("type") != "welcome":
            print("\n  ACCOUNT CREATION FAILED" if mode == "2" else "\n  SIGN-IN FAILED")
            print("  " + response.get("message", "Unknown error"))
            sock.close()
            input("\n  Press Enter to return...")
            return

        sock.setblocking(False)
        print("\n  Welcome, @" + username)
        print("  Opening JAYMOH CONNECT...")
        run_interface(sock, username, credential, host, initial_buffer)

    except (OSError, ConnectionError, json.JSONDecodeError) as error:
        if sock:
            sock.close()
        print("\n  CONNECTION ERROR")
        print("  " + str(error))
        input("\n  Press Enter to close...")
    finally:
        credential[:] = b"\x00" * len(credential)
        credential.clear()


def run_interface(sock, username, credential, host, initial_buffer=b""):
    tabs = ["CHATS", "GROUPS", "PROFILE", "SETTINGS", "MORE"]
    more_actions = [
        "New Chat", "Search Conversations", "Refresh Conversations",
        "Clear Search", "Create Group", "Add Member to Group",
        "See Group Members", "Sign Out"
    ]
    active_tab = 0

    inbox = []
    user_presence = {}
    chats = {}
    active_chat = None
    groups = []
    selected_group_id = None
    active_group = None
    group_chats = {}
    members_view = None
    group_members = {}
    members_error = None
    members_loading_since = None
    remove_confirmation = None

    selected = 0
    members_scroll = 0
    scroll = 0
    input_text = ""
    input_mode = None
    search_text = ""
    notice = "Welcome back, @" + username
    connected = True
    connection_state = "CONNECTED"
    network_buffer = initial_buffer
    retry_delays = (1, 2, 4, 8, 15)
    retry_index = 0
    next_reconnect_at = None
    reauth_required = False

    def request_inbox():
        try:
            send(sock, {"type": "inbox"})
        except OSError:
            pass

    def request_groups():
        try:
            send(sock, {"type": "groups"})
        except OSError:
            pass

    def mark_unconfirmed_messages():
        for conversation in chats.values():
            for message in conversation:
                if message.get("from") == username and not message.get("id"):
                    message["delivery_state"] = "unknown"
        for conversation in group_chats.values():
            for message in conversation:
                if message.get("from") == username and not message.get("id"):
                    message["delivery_state"] = "unknown"

    def connection_lost(reason):
        nonlocal connected, connection_state, network_buffer
        nonlocal retry_index, next_reconnect_at, notice
        if not connected:
            return
        mark_unconfirmed_messages()
        connected = False
        connection_state = "DISCONNECTED"
        network_buffer = b""
        retry_index = 0
        next_reconnect_at = time.monotonic() + retry_delays[retry_index]
        notice = (
            str(reason) + ". Retrying in 1 second; press ESC to cancel."
        )
        try:
            sock.close()
        except OSError:
            pass

    def attempt_reconnect():
        nonlocal sock, connected, connection_state, network_buffer
        nonlocal retry_index, next_reconnect_at, reauth_required, notice
        nonlocal members_error, members_loading_since
        candidate = None
        try:
            candidate = socket.create_connection((host, PORT), timeout=3)
            candidate.settimeout(5)
            send(candidate, {
                "type": "login",
                "username": username,
                "password": credential.decode("utf-8")
            })

            raw = b""
            while b"\n" not in raw:
                chunk = candidate.recv(4096)
                if not chunk:
                    raise ConnectionError("Server closed during authentication")
                raw += chunk

            response_line, remainder = raw.split(b"\n", 1)
            response = json.loads(response_line.decode())
            if response.get("type") != "welcome":
                message = response.get("message", "Authentication failed.")
                candidate.close()
                candidate = None
                if message in ("Incorrect password.", "Account does not exist."):
                    reauth_required = True
                    connection_state = "DISCONNECTED"
                    notice = "Authentication failed. Enter your password to retry."
                    return "reauth"
                raise ConnectionError("Reconnect rejected: " + message)

            candidate.setblocking(False)
            old_sock = sock
            sock = candidate
            candidate = None
            network_buffer = remainder
            connected = True
            connection_state = "CONNECTED"
            retry_index = 0
            next_reconnect_at = None
            try:
                old_sock.close()
            except (AttributeError, OSError):
                pass

            request_inbox()
            request_groups()
            for other in list(chats):
                try:
                    send(sock, {"type": "history", "with": other})
                except OSError:
                    break
            for group_id in list(group_chats):
                try:
                    send(sock, {"type": "group_history", "group_id": group_id})
                except OSError:
                    break
            if members_view:
                try:
                    send(sock, {
                        "type": "group_members",
                        "group_id": members_view["id"]
                    })
                    group_members.pop(members_view["id"], None)
                    members_loading_since = time.monotonic()
                    members_error = None
                except OSError:
                    members_error = "Could not refresh members after reconnect."
                    members_loading_since = None
            notice = "Reconnected. Restoring conversations and groups..."
            return "connected"

        except (OSError, ConnectionError, json.JSONDecodeError) as error:
            if candidate:
                candidate.close()
            connected = False
            connection_state = "DISCONNECTED"
            retry_index = min(retry_index + 1, len(retry_delays) - 1)
            delay = retry_delays[retry_index]
            next_reconnect_at = time.monotonic() + delay
            notice = f"Reconnect failed: {error}. Retrying in {delay} seconds."
            return "retry"

    def open_chat(other):
        nonlocal active_chat, scroll, input_text, input_mode, notice
        other = other.lower().strip().lstrip("@")

        if not other or other == username:
            notice = "Choose a different username."
            return

        active_chat = other
        chats.setdefault(other, [])
        scroll = 0
        input_text = ""
        input_mode = None

        try:
            send(sock, {"type": "history", "with": other})
            notice = "Opening conversation with @" + other
        except OSError:
            notice = "Could not contact server."

    def open_group(group):
        nonlocal active_group, scroll, input_text, input_mode, notice
        active_group = {
            "id": group.get("id"),
            "name": group.get("name", "Group")
        }
        group_id = active_group["id"]
        group_chats.setdefault(group_id, [])
        scroll = 0
        input_text = ""
        input_mode = None

        try:
            send(sock, {"type": "group_history", "group_id": group_id})
            notice = "Opening group: " + active_group["name"]
        except OSError:
            notice = "Could not contact server."

    def upsert_inbox(other, body, sender, created):
        for item in inbox:
            if item["username"] == other:
                item["last_text"] = body
                item["last_sender"] = sender
                item["time"] = created
                inbox.remove(item)
                inbox.insert(0, item)
                return

        inbox.insert(0, {
            "username": other,
            "last_text": body,
            "last_sender": sender,
            "time": created,
            "unread": 0,
            "online": False
        })

    def set_presence(other, is_online, seen_at=None):
        if not other:
            return
        previous = user_presence.get(other, {})
        user_presence[other] = {
            "online": bool(is_online),
            "last_seen": seen_at if seen_at is not None else previous.get("last_seen")
        }

    def merge_uncertain_messages(local_messages, server_messages):
        merged = list(server_messages)
        used_server = set()
        for local_message in local_messages:
            if local_message.get("delivery_state") != "unknown":
                continue
            local_time = local_message.get("local_time")
            if not local_time:
                merged.append(local_message)
                continue
            matched = False
            for index, remote_message in enumerate(server_messages):
                if index in used_server:
                    continue
                if (remote_message.get("from") != local_message.get("from")
                        or remote_message.get("text") != local_message.get("text")):
                    continue
                remote_time = remote_message.get("time")
                try:
                    local_dt = datetime.fromisoformat(local_time.replace("Z", "+00:00"))
                    remote_dt = datetime.fromisoformat(remote_time.replace("Z", "+00:00"))
                    close_in_time = abs((local_dt - remote_dt).total_seconds()) <= 120
                except (ValueError, TypeError):
                    close_in_time = False
                if close_in_time:
                    used_server.add(index)
                    matched = True
                    break
            if not matched:
                merged.append(local_message)
        return merged

    def handle_packet(data):
        nonlocal inbox, groups, selected, selected_group_id, active_group, notice
        nonlocal user_presence
        nonlocal members_error, members_loading_since, remove_confirmation

        kind = data.get("type")

        if kind == "presence_ping":
            try:
                send(sock, {"type": "presence_pong"})
            except OSError:
                pass

        elif kind == "presence_snapshot":
            online_names = set(data.get("online_users", []))
            seen_map = data.get("last_seen", {})
            user_presence = {}
            for other, seen_at in seen_map.items():
                set_presence(other, False, seen_at)
            for other in online_names:
                set_presence(other, True, seen_map.get(other))

        elif kind == "presence_change":
            other = data.get("username", "")
            set_presence(other, data.get("online", False), data.get("last_seen"))
            for item in inbox:
                if item.get("username") == other:
                    item["online"] = bool(data.get("online"))
                    item["last_seen"] = data.get("last_seen")

        elif kind == "inbox":
            inbox = data.get("conversations", [])
            online_names = set(data.get("online_users", []))
            seen_map = data.get("last_seen", {})
            for other, seen_at in seen_map.items():
                if other not in online_names:
                    set_presence(other, False, seen_at)
            for other in online_names:
                set_presence(other, True, seen_map.get(other))

            for item in inbox:
                other = item["username"]
                item["online"] = other in online_names
                item["last_seen"] = seen_map.get(other, item.get("last_seen"))
                set_presence(other, item["online"], item.get("last_seen"))

            selected = min(selected, max(0, len(inbox) - 1))

        elif kind == "message":
            other = data.get("from", "")
            body = data.get("text", "")
            created = data.get("time", "")

            chats.setdefault(other, []).append({
                "id": data.get("id"),
                "from": other,
                "text": body,
                "time": created,
                "delivered": True
            })

            if active_chat == other:
                try:
                    send(sock, {"type": "history", "with": other})
                except OSError:
                    pass
            else:
                for item in inbox:
                    if item["username"] == other:
                        item["unread"] = item.get("unread", 0) + 1

            upsert_inbox(other, body, other, created)
            notice = "New message from @" + other

        elif kind == "history":
            other = data.get("with", "")
            peer_presence = data.get("presence")
            if isinstance(peer_presence, dict):
                set_presence(other, peer_presence.get("online", False),
                             peer_presence.get("last_seen"))
            chats[other] = merge_uncertain_messages(
                chats.get(other, []), data.get("messages", [])
            )

            if active_chat == other:
                notice = "Conversation with @" + other

            for item in inbox:
                if item["username"] == other:
                    item["unread"] = 0

        elif kind == "sent":
            msg_id = data.get("id")
            recipient = data.get("to", "")

            for message in reversed(chats.get(recipient, [])):
                if not message.get("id"):
                    message["id"] = msg_id
                    message["time"] = data.get("time", "")
                    message["delivery_state"] = "sent"
                    break

            notice = "Message sent."

        elif kind in ("delivered", "read"):
            msg_id = data.get("id")

            for conversation in chats.values():
                for message in conversation:
                    if message.get("id") == msg_id:
                        if kind == "delivered":
                            message["delivered"] = True
                            message["delivery_state"] = "delivered"
                        else:
                            message["delivered"] = True
                            message["read_at"] = "live"
                            message["delivery_state"] = "read"
                        break

        elif kind == "groups":
            groups = data.get("groups", [])
            selected = min(selected, max(0, len(groups) - 1))
            if groups and selected_group_id is None:
                selected_group_id = groups[0].get("id")

        elif kind == "group_created":
            selected_group_id = data.get("group_id")
            notice = "Group created: " + data.get("name", "")
            request_groups()

        elif kind == "group_member_added":
            notice = "Added @" + data.get("username", "") + " to the group."
            request_groups()

        elif kind == "group_invited":
            notice = "You were added to a group."
            request_groups()

        elif kind == "group_message":
            group_id = data.get("group_id")
            sender = data.get("from", data.get("sender", ""))
            body = data.get("text", data.get("body", ""))
            created = data.get("time", "")

            if group_id is not None:
                group_chats.setdefault(group_id, []).append({
                    "id": data.get("id"),
                    "from": sender,
                    "text": body,
                    "time": created
                })

                for item in groups:
                    if item.get("id") == group_id:
                        item["last_message"] = body
                        break

                if not active_group or active_group.get("id") != group_id:
                    notice = "New group message in " + next(
                        (g.get("name", "a group") for g in groups
                         if g.get("id") == group_id),
                        "a group"
                    )

        elif kind == "group_members":
            group_id = data.get("group_id")
            if group_id is not None:
                group_members[group_id] = data.get("members", [])
                for member in group_members[group_id]:
                    set_presence(member.get("username"), member.get("online", False),
                                 member.get("last_seen"))
                selected = min(selected, max(0, len(group_members[group_id]) - 1))
                members_loading_since = None
                members_error = None
                if members_view and members_view.get("id") == group_id:
                    notice = "Members loaded: " + str(
                        len(group_members[group_id])
                    )

        elif kind == "group_member_removed":
            group_id = data.get("group_id")
            removed_username = data.get("username", "")
            remove_confirmation = None
            if group_id is not None:
                group_members.pop(group_id, None)
                if members_view and members_view.get("id") == group_id:
                    try:
                        send(sock, {"type": "group_members", "group_id": group_id})
                        members_loading_since = time.monotonic()
                        members_error = None
                        notice = "Removed @" + removed_username + ". Refreshing members..."
                    except OSError:
                        members_error = "Could not refresh members. Press ESC and reopen."
                        members_loading_since = None
                        notice = members_error
                else:
                    notice = "Removed @" + removed_username + " from the group."

        elif kind == "group_member_removed_notice":
            group_id = data.get("group_id")
            groups = [group for group in groups if group.get("id") != group_id]
            if selected_group_id == group_id:
                selected_group_id = groups[0].get("id") if groups else None
            if active_group and active_group.get("id") == group_id:
                active_group = None
            if members_view and members_view.get("id") == group_id:
                group_members.pop(group_id, None)
                members_error = "You were removed from this group."
                members_loading_since = None
            notice = "You were removed from a group."

        elif kind == "group_history":
            group_id = data.get("group_id")
            if group_id is not None:
                group_chats[group_id] = merge_uncertain_messages(
                    group_chats.get(group_id, []), data.get("messages", [])
                )
                if active_group and active_group.get("id") == group_id:
                    notice = "Group conversation: " + active_group.get("name", "")

        elif kind == "group_sent":
            group_id = data.get("group_id")
            if group_id is not None:
                messages = group_chats.setdefault(group_id, [])
                for message in reversed(messages):
                    if message.get("from") == username and not message.get("id"):
                        message["id"] = data.get("id")
                        message["time"] = data.get("time", "")
                        message["delivery_state"] = "sent"
                        break
            notice = "Group message sent."

        elif kind == "error":
            notice = "Server: " + data.get("message", "Unknown error")
            if members_view and members_loading_since is not None:
                members_error = notice
                members_loading_since = None
            remove_confirmation = None

        elif kind == "goodbye":
            notice = "Disconnected."

    def draw(stdscr, visible_inbox):
        nonlocal selected, members_scroll
        stdscr.erase()
        height, width = stdscr.getmaxyx()

        green = curses.color_pair(1) | curses.A_BOLD
        muted = curses.color_pair(2)
        selected_attr = curses.color_pair(3) | curses.A_BOLD
        white = curses.color_pair(4)

        try:
            curses.curs_set(1 if active_chat or active_group else 0)
        except curses.error:
            pass

        def put(y, x, value, attr=0, limit=None):
            if y < 0 or y >= height or x < 0 or x >= width - 1:
                return
            available = width - x - 1
            if limit is not None:
                available = min(available, limit)
            if available <= 0:
                return
            try:
                stdscr.addnstr(y, x, str(value), available, attr)
            except curses.error:
                pass

        # Group members screen
        if members_view:
            try:
                curses.curs_set(0)
            except curses.error:
                pass

            compact_members_view = height < 10
            if compact_members_view:
                put(0, 2, "GROUP MEMBERS", green)
                put(0, max(2, width - 23), connection_state,
                    green if connected else muted)
                data_top = 2
            else:
                put(0, 2, "JAYMOH CONNECT", green)
                put(0, max(2, width - 23), connection_state,
                    green if connected else muted)
                put(2, 2, "GROUP MEMBERS", green)
                put(3, 2, members_view.get("name", "Group"), muted)
                put(4, 1, "─" * (width - 3), muted)
                data_top = 8 if height >= 15 else 5

            current_members = group_members.get(members_view["id"])

            if members_error:
                put(7, 4, members_error, muted)
            elif current_members is None:
                put(7, 4, "Loading group members...", muted)
            elif not current_members:
                put(7, 4, "No members found.", muted)
            else:
                role_x = max(25, width - 18)
                if height >= 15:
                    put(6, 4, "MEMBER", muted)
                    put(6, role_x, "ROLE", muted)

                presence_row_height = 2 if width < 100 else 1
                available_height = max(
                    1, height - data_top - (4 if remove_confirmation else 3)
                )
                visible_rows = max(1, available_height // presence_row_height)
                selected = min(max(0, selected), len(current_members) - 1)
                max_scroll = max(0, len(current_members) - visible_rows)
                members_scroll = min(max(0, members_scroll), max_scroll)
                if selected < members_scroll:
                    members_scroll = selected
                elif selected >= members_scroll + visible_rows:
                    members_scroll = selected - visible_rows + 1

                for row_offset, member in enumerate(
                        current_members[members_scroll:members_scroll + visible_rows]):
                    index = members_scroll + row_offset
                    y = data_top + row_offset * presence_row_height
                    if y >= height - (4 if remove_confirmation else 3):
                        break

                    member_name = str(member.get("username", "unknown"))
                    member_label = "@" + member_name
                    role = str(member.get("role", "member")).upper()
                    state = user_presence.get(member_name, {
                        "online": member.get("online"),
                        "last_seen": member.get("last_seen")
                    })
                    if state.get("online") is True:
                        state_text = "● ONLINE"
                        state_attr = green
                    elif state.get("online") is False:
                        state_text = "○ OFFLINE · LAST SEEN " + presence_time_label(
                            state.get("last_seen")
                        )
                        state_attr = muted
                    else:
                        state_text = "PRESENCE UNKNOWN"
                        state_attr = muted

                    attr = selected_attr if index == selected else white
                    put(y, 4, member_label, attr)
                    put(y, role_x, role, green)
                    if presence_row_height == 1:
                        state_x = 4 + len(member_label) + 2
                        put(y, state_x, state_text, state_attr,
                            max(0, role_x - state_x - 2))
                    else:
                        put(y + 1, 6, state_text, state_attr,
                            width - 8)

            if remove_confirmation:
                put(height - 4, 2,
                    "Remove @" + remove_confirmation["username"] + "?  Y Confirm  N/ESC Cancel",
                    selected_attr)
            else:
                put(height - 2, 2, "ENTER Remove member    ESC Back to groups", muted)
            stdscr.refresh()
            return

        # Application header
        put(0, 2, "JAYMOH CONNECT", green)
        put(0, max(2, width - 24), connection_state,
            green if connected else muted)
        put(1, 2, "YOUR MESSAGING SPACE", muted)
        put(2, 1, "─" * (width - 3), muted)

        # Main navigation is hidden inside an open conversation.
        if not active_chat and not active_group:
            x = 2
            for index, tab in enumerate(tabs):
                label = f" {tab} "
                attr = selected_attr if index == active_tab else muted
                put(3, x, label, attr)
                x += len(label) + 3

            put(4, 1, "─" * (width - 3), muted)

        # Narrow terminal
        if width < 76:
            if active_group:
                draw_conversation(
                    stdscr, put, height, width, active_group["name"],
                    group_chats, input_text, input_mode, notice,
                    muted, green, white, scroll,
                    group=True, chat_key=active_group["id"]
                )
            elif active_chat:
                draw_conversation(
                    stdscr, put, height, width, active_chat,
                    chats, input_text, input_mode, notice,
                    muted, green, white, scroll
                )
            else:
                draw_tab_content(
                    stdscr, put, height, width, active_tab,
                    visible_inbox, selected, username, notice,
                    muted, green, selected_attr
                )

        # Full-width conversation workspace
        elif active_group:
            draw_conversation(
                stdscr, put, height, width, active_group["name"],
                group_chats, input_text, input_mode, notice,
                muted, green, white, scroll,
                group=True, chat_key=active_group["id"]
            )

        elif active_chat:
            draw_conversation(
                stdscr, put, height, width, active_chat,
                chats, input_text, input_mode, notice,
                muted, green, white, scroll
            )

        # Full-width conversation list
        elif active_tab == 0:
            put(5, 2, "YOUR CONVERSATIONS", green)
            put(6, 2, "People and messages", muted)
            put(7, 1, "─" * (width - 3), muted)

            if not visible_inbox:
                put(10, 3, "No conversations yet.", muted)
                put(12, 3, "Start a new chat from MORE.", muted)

            for index, item in enumerate(visible_inbox):
                y = 9 + index * 4
                if y >= height - 5:
                    break

                other = item["username"]
                name = "@" + other
                state = user_presence.get(other)
                if state is None:
                    state = {
                        "online": item.get("online"),
                        "last_seen": item.get("last_seen")
                    }
                is_online = state.get("online")
                unread = item.get("unread", 0)
                marker = f"  [{unread} unread]" if unread else ""
                attr = selected_attr if index == selected else 0
                indicator = "●" if is_online else "○"
                indicator_attr = green if is_online else muted
                put(y, 3, indicator, indicator_attr)
                put(y, 5, name + marker, attr, width - 10)

                state_text = (
                    "ONLINE" if is_online is True else
                    "OFFLINE" if is_online is False else
                    "PRESENCE UNKNOWN"
                )
                state_x = 7 + len(name) + len(marker)
                put(y, state_x, state_text,
                    green if is_online is True else muted,
                    max(0, width - state_x - 2))

                preview = item.get("last_text", "").replace("\n", " ")
                if item.get("last_sender") == username:
                    preview = "You: " + preview
                put(y + 1, 7, preview or "No messages yet.",
                    muted, width - 12)

                if is_online is False:
                    last_seen = presence_time_label(state.get("last_seen"))
                    put(y + 2, 7, "LAST SEEN " + last_seen, muted,
                        width - 12)
                elif is_online is None:
                    put(y + 2, 7, "PRESENCE UNKNOWN", muted,
                        width - 12)
                else:
                    put(y + 2, 3, "─" * (width - 7), muted)
                put(y + 3, 3, "─" * (width - 7), muted)

        # Empty Chats and other sections use the full width
        else:
            draw_tab_content(
                stdscr, put, height, width, active_tab,
                visible_inbox, selected, username, notice,
                muted, green, selected_attr
            )

        # Dedicated group picker for adding members.
        if input_mode in ("group_pick", "members_pick"):
            picker_title = (
                "SEE GROUP MEMBERS"
                if input_mode == "members_pick"
                else "ADD MEMBER TO GROUP"
            )
            put(5, 2, picker_title, green)
            put(6, 2, "Choose a group", muted)
            put(7, 1, "─" * (width - 3), muted)

            if not groups:
                put(10, 3, "No groups available.", muted)
            else:
                for index, group in enumerate(groups):
                    y = 9 + index * 3
                    if y >= height - 6:
                        break
                    attr = selected_attr if index == selected else muted
                    marker = "› " if index == selected else "  "
                    name = group.get("name", "Unnamed group")
                    count = group.get("member_count", 0)
                    put(y, 3, marker + name, attr, width - 8)
                    put(y + 1, 7, str(count) + " members", muted)

        # Keep the conversation screen free of unrelated navigation.
        if not active_chat and not active_group:
            if input_mode in ("new", "search", "group_new", "group_add"):
                put(height - 4, 2, "> " + input_text, white, width - 5)
            put(height - 3, 2, notice, muted, width - 5)
            put(height - 2, 2,
                "← → Switch Section    ↑ ↓ Select    ENTER Open", muted)
            put(height - 1, 2,
                "ESC Back    MORE for Actions", muted)

        stdscr.refresh()

    def draw_tab_content(stdscr, put, height, width, tab, items,
                         selected_index, account, notice, muted, green,
                         selected_attr, left=2):
        if tab == 0:
            put(9, left, "YOUR RECENT CONVERSATIONS", green)
            if not items:
                put(12, left, "Your conversations will appear here.", muted)
                put(14, left, "Start a conversation from MORE.", muted)
            else:
                item = items[min(selected_index, len(items) - 1)]
                put(12, left, "@" + item["username"], selected_attr)
                put(14, left, item.get("last_text", ""), muted)
                put(16, left, "Select a conversation from the left panel.", muted)

        elif tab == 1:
            put(9, left, "YOUR GROUPS", green)
            put(10, left, "Groups you belong to", muted)

            if not groups:
                put(13, left, "You are not a member of any groups yet.", muted)
                put(15, left, "Open MORE and choose Create Group to get started.", muted)
            else:
                for i, group in enumerate(groups):
                    y = 13 + i * 3
                    if y >= height - 7:
                        break
                    attr = selected_attr if i == selected_index else muted
                    marker = "› " if i == selected_index else "  "
                    name = group.get("name", "Unnamed group")
                    count = group.get("member_count", 0)
                    put(y, left, marker + name, attr, width - left - 3)
                    put(
                        y + 1, left + 2,
                        f"{count} members  ·  Created by @{group.get('created_by', '')}",
                        muted
                    )

                chosen = groups[min(selected_index, len(groups) - 1)]
                put(height - 6, left, "Selected: " + chosen.get("name", ""), green)

        elif tab == 2:
            put(9, left, "YOUR PROFILE", green)
            put(12, left, "Username", muted)
            put(13, left, "@" + account, selected_attr)
            put(16, left, "Profile editing will be added in a later stage.", muted)

        elif tab == 3:
            put(9, left, "SETTINGS", green)
            put(12, left, "Account: @" + account, muted)
            put(14, left, "Appearance and notification preferences are planned.", muted)

        elif tab == 4:
            put(9, left, "QUICK ACTIONS", green)
            put(11, left, "Use the arrow keys and press ENTER to select.", muted)
            for i, action in enumerate(more_actions):
                y = 14 + i * 3
                attr = selected_attr if i == selected_index else muted
                marker = "› " if i == selected_index else "  "
                put(y, left, marker + action, attr)
            put(14 + len(more_actions) * 3 + 1, left, "ESC returns to the previous screen.", muted)

    def draw_conversation(stdscr, put, height, width, other, chats,
                          draft, mode, notice, muted, green, white,
                          scroll, left=2, group=False, chat_key=None):
        right_width = max(1, width - left - 3)

        # Conversation header
        put(5, left, other if group else "@" + other, green)
        if group:
            put(6, left, "GROUP CHAT", muted)
            put(5, max(left + 2, width - 22),
                "SERVER " + connection_state,
                green if connected else muted)
        else:
            peer_state = user_presence.get(other)
            if peer_state is None or peer_state.get("online") is None:
                header_status = "PRIVATE CHAT · PRESENCE UNKNOWN"
                status_attr = muted
            elif peer_state.get("online"):
                header_status = "PRIVATE CHAT · ● ONLINE"
                status_attr = green
            else:
                header_status = (
                    "PRIVATE · ○ OFFLINE · LAST SEEN "
                    + presence_time_label(peer_state.get("last_seen"))
                )
                status_attr = muted
            put(6, left, header_status, status_attr)
            put(5, max(left + 2, width - 22),
                "SERVER " + connection_state,
                green if connected else muted)
        put(7, left, "─" * right_width, muted)

        # Message timeline
        timeline_top = 9
        timeline_bottom = height - 10
        timeline_space = max(1, timeline_bottom - timeline_top)

        messages = chats.get(chat_key if chat_key is not None else other, [])
        end = max(0, len(messages) - scroll)

        max_bubble_width = max(16, min(62, right_width - 8))
        inner_limit = max(10, max_bubble_width - 4)

        blocks = []

        for message in messages[:end]:
            sender = message.get("from", "")
            own = sender == username
            label = "YOU" if own else "@" + sender
            stamp = time_label(message.get("time", ""))
            body = str(message.get("text", ""))

            wrapped = textwrap.wrap(body, inner_limit) or [""]
            wrapped = wrapped[:max(1, timeline_space - 3)]

            bubble_width = min(
                max_bubble_width,
                max(16, max(len(line) for line in wrapped) + 4)
            )

            block = [
                (label + "  " + stamp, green if own else muted, None),
                ("┌" + "─" * (bubble_width - 2) + "┐", muted, None)
            ]

            for line in wrapped:
                block.append((
                    "│ " + line.ljust(bubble_width - 4) + " │",
                    white if own else 0,
                    None
                ))

            block.append((
                "└" + "─" * (bubble_width - 2) + "┘",
                muted,
                None
            ))

            if own and not group:
                if message.get("delivery_state") == "unknown":
                    status = "Delivery unknown · not resent"
                elif message.get("read_at"):
                    status = "✓✓ Read"
                elif message.get("delivered"):
                    status = "✓✓ Delivered"
                elif message.get("id"):
                    status = "✓ Sent"
                else:
                    status = "Sending..."

                block.append((status, muted, None))
            elif own and group and message.get("delivery_state") == "unknown":
                block.append(("Delivery unknown · not resent", muted, None))

            block.append(("", 0, None))

            blocks.append((block, own, bubble_width))

        # Select the newest messages that fit in the timeline.
        visible_blocks = []
        used_rows = 0

        for block, own, bubble_width in reversed(blocks):
            if used_rows + len(block) > timeline_space:
                if not visible_blocks and timeline_space >= 4:
                    block = block[-timeline_space:]
                else:
                    break

            visible_blocks.append((block, own, bubble_width))
            used_rows += len(block)

        visible_blocks.reverse()

        y = timeline_top

        if not messages:
            put(y + 2, left + 2, "No messages yet.", muted)
            put(y + 4, left + 2,
                "Start the conversation below.", muted)

        for block, own, bubble_width in visible_blocks:
            if y >= timeline_bottom:
                break

            bubble_x = (
                left + right_width - bubble_width
                if own else left + 2
            )
            bubble_x = max(left, min(bubble_x, width - bubble_width - 2))

            for text, attr, unused in block:
                if y >= timeline_bottom:
                    break
                if text:
                    put(y, bubble_x, text, attr, bubble_width)
                y += 1

        # Message composer
        top = height - 8
        input_y = height - 7
        bottom = height - 6
        hint_y = height - 5

        border_width = max(1, right_width - 2)

        put(top, left, "┌" + "─" * border_width + "┐", muted)
        put(bottom, left, "└" + "─" * border_width + "┘", muted)

        put(input_y, left, "│", muted)
        put(input_y, left + right_width - 1, "│", muted)

        inner_width = max(1, right_width - 7)
        visible_draft = draft[-inner_width:]

        if draft:
            put(input_y, left + 3, "› " + visible_draft,
                white, inner_width + 2)
        else:
            put(input_y, left + 3, "› Write a message...",
                muted, inner_width + 2)

        put(hint_y, left,
            "ENTER Send    ESC Back    UP/DOWN Scroll", muted)

        cursor_x = min(left + 5 + len(visible_draft), width - 2)
        try:
            stdscr.move(input_y, cursor_x)
        except curses.error:
            pass

    def interface(stdscr):
        nonlocal active_tab, active_chat, active_group, selected, scroll, selected_group_id
        nonlocal input_text, input_mode, search_text
        nonlocal connected, network_buffer, notice
        nonlocal members_view, members_error, members_loading_since
        nonlocal members_scroll, connection_state
        nonlocal remove_confirmation

        curses.curs_set(0)
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_GREEN)
        curses.init_pair(4, curses.COLOR_WHITE, -1)

        stdscr.keypad(True)
        stdscr.timeout(100)

        if connected:
            request_inbox()
            request_groups()
        running = True

        while running:
            reconnect_now = False
            if (not connected and not reauth_required
                    and next_reconnect_at is not None
                    and time.monotonic() >= next_reconnect_at):
                connection_state = "RECONNECTING..."
                notice = "Reconnecting to the server..."
                reconnect_now = True
            if (members_loading_since is not None
                    and time.monotonic() - members_loading_since > 10):
                members_loading_since = None
                members_error = "Request timed out. Press ESC and reopen members."
                notice = members_error

            if search_text or input_mode == "search":
                query = input_text if input_mode == "search" else search_text
                visible = [
                    item for item in inbox
                    if query.lower() in item["username"].lower()
                    or query.lower() in item.get("last_text", "").lower()
                ]
            else:
                visible = inbox[:]

            draw(stdscr, visible)

            if reconnect_now:
                # Draw RECONNECTING... before the bounded socket/auth attempt.
                stdscr.refresh()

            try:
                key = stdscr.get_wch()
            except curses.error:
                key = None

            if key is not None:
                if key == "\x03" or (key == "\x1b" and not connected):
                    running = False

                elif key == curses.KEY_LEFT and not active_chat and not active_group and not input_mode:
                    active_tab = (active_tab - 1) % len(tabs)
                    selected = 0

                elif key == curses.KEY_RIGHT and not active_chat and not active_group and not input_mode:
                    active_tab = (active_tab + 1) % len(tabs)
                    selected = 0

                elif key in ("\n", "\r", curses.KEY_ENTER):
                    if not connected and not (active_chat or active_group):
                        notice = "Offline. Waiting to reconnect; ESC exits."
                    elif members_view:
                        member_list = group_members.get(members_view["id"], [])
                        if remove_confirmation:
                            pass
                        elif member_list:
                            selected = min(selected, len(member_list) - 1)
                            member = member_list[selected]
                            if member.get("role") == "admin":
                                notice = "Group admins cannot be removed."
                            else:
                                remove_confirmation = {"username": member.get("username", "")}
                    elif input_mode == "new":
                        other = input_text.strip()
                        input_text = ""
                        input_mode = None
                        open_chat(other)

                    elif input_mode == "search":
                        input_mode = None
                        search_text = input_text
                        input_text = ""
                        selected = 0

                    elif input_mode == "group_new":
                        group_name = input_text.strip()
                        input_text = ""
                        input_mode = None
                        if group_name:
                            send(sock, {"type": "group_create", "name": group_name})
                            notice = "Creating group..."
                        else:
                            notice = "Group name cannot be empty."

                    elif input_mode == "members_pick":
                        if groups:
                            selected = min(selected, len(groups) - 1)
                            chosen_group = groups[selected]
                            group_id = chosen_group.get("id")
                            if group_id is not None:
                                members_view = {
                                    "id": group_id,
                                    "name": chosen_group.get("name", "Group")
                                }
                                input_mode = None
                                selected = 0
                                members_scroll = 0
                                group_members.pop(group_id, None)
                                members_error = None
                                try:
                                    send(sock, {"type": "group_members", "group_id": group_id})
                                    members_loading_since = time.monotonic()
                                    notice = "Loading group members..."
                                except OSError:
                                    members_error = "Could not contact server. Press ESC and reopen."
                                    members_loading_since = None
                                    notice = members_error
                            else:
                                input_mode = None
                                notice = "Invalid group."
                        else:
                            input_mode = None
                            notice = "No groups available."

                    elif input_mode == "group_pick":
                        if groups:
                            selected = min(selected, len(groups) - 1)
                            chosen_group = groups[selected]
                            selected_group_id = chosen_group.get("id")
                            input_mode = "group_add"
                            input_text = ""
                            notice = (
                                "Adding member to "
                                + chosen_group.get("name", "group")
                                + ". Enter username."
                            )
                        else:
                            input_mode = None
                            notice = "No groups available."

                    elif input_mode == "group_add":
                        member_name = input_text.strip().lower().lstrip("@")
                        input_text = ""
                        input_mode = None
                        if member_name and selected_group_id:
                            send(sock, {
                                "type": "group_add",
                                "group_id": selected_group_id,
                                "username": member_name
                            })
                            notice = "Adding @" + member_name + "..."
                        else:
                            notice = "Enter a valid username."

                    elif active_group:
                        body = input_text.strip()
                        input_mode = "message"

                        if body and not connected:
                            notice = "Offline. Message text kept; it has not been sent."
                        elif body:
                            message = {
                                "from": username,
                                "text": body,
                                "time": "",
                                "local_time": datetime.now().astimezone().isoformat(),
                                "id": None,
                                "delivery_state": "awaiting_ack"
                            }
                            try:
                                send(sock, {
                                    "type": "group_message",
                                    "group_id": active_group["id"],
                                    "text": body
                                })
                                group_chats.setdefault(
                                    active_group["id"], []
                                ).append(message)
                                input_text = ""
                                scroll = 0
                            except OSError as error:
                                input_text = body
                                notice = "Group send status uncertain; text kept. " + str(error)
                                connection_lost("Connection lost while sending")
                        else:
                            input_text = ""

                    elif active_chat:
                        body = input_text.strip()
                        input_mode = "message"

                        if body and not connected:
                            notice = "Offline. Message text kept; it has not been sent."
                        elif body:
                            message = {
                                "from": username,
                                "text": body,
                                "time": "",
                                "local_time": datetime.now().astimezone().isoformat(),
                                "id": None,
                                "delivered": False,
                                "delivery_state": "awaiting_ack"
                            }
                            try:
                                send(sock, {
                                    "type": "message",
                                    "to": active_chat,
                                    "text": body
                                })
                                chats.setdefault(active_chat, []).append(message)
                                input_text = ""
                                upsert_inbox(
                                    active_chat, body, username, ""
                                )
                                scroll = 0
                            except OSError as error:
                                input_text = body
                                notice = "Send status uncertain; text kept. " + str(error)
                                connection_lost("Connection lost while sending")
                        else:
                            input_text = ""

                    elif active_tab == 4:
                        action = more_actions[min(selected, len(more_actions) - 1)]
                        if action == "New Chat":
                            input_mode = "new"
                            input_text = ""
                            notice = "Enter the username to start a conversation."
                        elif action == "Search Conversations":
                            input_mode = "search"
                            input_text = search_text
                            notice = "Type a name or message to search."
                        elif action == "Refresh Conversations":
                            request_inbox()
                            notice = "Refreshing conversations..."
                        elif action == "Clear Search":
                            search_text = ""
                            input_text = ""
                            selected = 0
                            notice = "Search cleared."
                        elif action == "Create Group":
                            input_mode = "group_new"
                            input_text = ""
                            notice = "Enter a group name, then press ENTER."
                        elif action == "See Group Members":
                            if groups:
                                selected = 0
                                input_mode = "members_pick"
                                input_text = ""
                                notice = "Choose a group to view its members."
                            else:
                                request_groups()
                                notice = "Loading your groups..."
                        elif action == "Add Member to Group":
                            if groups:
                                selected = 0
                                input_mode = "group_pick"
                                input_text = ""
                                notice = "Choose a group with UP/DOWN."
                            else:
                                request_groups()
                                notice = "Loading your groups..."
                        elif action == "Sign Out":
                            running = False

                    elif active_tab == 0 and visible:
                        open_chat(visible[selected]["username"])

                    elif active_tab == 1 and groups:
                        selected = min(selected, len(groups) - 1)
                        selected_group_id = groups[selected].get("id")
                        open_group(groups[selected])

                elif key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
                    if input_mode in ("new", "search", "message", "group_new", "group_add"):
                        input_text = input_text[:-1]
                        if input_mode == "search":
                            search_text = input_text

                elif key == curses.KEY_UP:
                    if members_view and not remove_confirmation:
                        selected = max(0, selected - 1)
                    elif input_mode in ("group_pick", "members_pick"):
                        selected = max(0, selected - 1)
                    elif active_group:
                        scroll = min(
                            max(0, len(group_chats.get(active_group["id"], [])) - 1),
                            scroll + 1
                        )
                    elif active_chat:
                        scroll = min(
                            max(0, len(chats.get(active_chat, [])) - 1),
                            scroll + 1
                        )
                    elif active_tab in (0, 1):
                        selected = max(0, selected - 1)
                        if active_tab == 1 and groups:
                            selected_group_id = groups[min(selected, len(groups) - 1)].get("id")
                    elif active_tab == 4:
                        selected = max(0, selected - 1)

                elif key == curses.KEY_DOWN:
                    if members_view and not remove_confirmation:
                        member_list = group_members.get(members_view["id"], [])
                        selected = min(max(0, len(member_list) - 1), selected + 1)
                    elif input_mode in ("group_pick", "members_pick"):
                        selected = min(max(0, len(groups) - 1), selected + 1)
                    elif active_group:
                        scroll = max(0, scroll - 1)
                    elif active_chat:
                        scroll = max(0, scroll - 1)
                    elif active_tab == 0:
                        selected = min(max(0, len(visible) - 1), selected + 1)
                    elif active_tab == 1:
                        selected = min(max(0, len(groups) - 1), selected + 1)
                        if groups:
                            selected_group_id = groups[selected].get("id")
                    elif active_tab == 4:
                        selected = min(len(more_actions) - 1, selected + 1)

                elif key == "\x1b":
                    if remove_confirmation:
                        remove_confirmation = None
                        notice = "Removal cancelled."
                    elif members_view:
                        members_view = None
                        members_error = None
                        members_loading_since = None
                        notice = "Returned to groups."
                    elif active_group:
                        active_group = None
                        input_mode = None
                        input_text = ""
                        scroll = 0
                        request_groups()
                    elif active_chat:
                        active_chat = None
                        input_mode = None
                        input_text = ""
                        scroll = 0
                        request_inbox()
                    elif input_mode:
                        input_mode = None
                        input_text = ""
                    elif search_text:
                        search_text = ""
                        selected = 0

                elif isinstance(key, str) and key.isprintable():
                    if members_view and remove_confirmation:
                        if key.lower() == "y":
                            try:
                                send(sock, {
                                    "type": "group_remove",
                                    "group_id": members_view["id"],
                                    "username": remove_confirmation["username"]
                                })
                                notice = "Removing @" + remove_confirmation["username"] + "..."
                            except OSError:
                                notice = "Could not contact server."
                            remove_confirmation = None
                        elif key.lower() == "n":
                            remove_confirmation = None
                            notice = "Removal cancelled."
                    elif members_view:
                        pass

                    elif active_group:
                        input_mode = "message"
                        if len(input_text) < 4000:
                            input_text += key

                    elif active_chat:
                        input_mode = "message"
                        if len(input_text) < 4000:
                            input_text += key

                    elif input_mode in ("new", "search", "group_new", "group_add"):
                        if len(input_text) < 40:
                            input_text += key
                            if input_mode == "search":
                                search_text = input_text

            if reconnect_now and running:
                result = attempt_reconnect()
                if result == "reauth":
                    running = False

            # Receive packets on the single active socket without blocking.
            if connected:
                try:
                    while b"\n" in network_buffer:
                        raw, network_buffer = network_buffer.split(b"\n", 1)
                        if raw:
                            handle_packet(json.loads(raw.decode("utf-8")))

                    readable, _, _ = select.select([sock], [], [], 0)
                    if readable:
                        chunk = sock.recv(8192)
                        if not chunk:
                            connection_lost("Connection to server lost")
                        else:
                            network_buffer += chunk
                            while b"\n" in network_buffer:
                                raw, network_buffer = network_buffer.split(b"\n", 1)
                                if raw:
                                    handle_packet(json.loads(raw.decode("utf-8")))

                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    connection_lost("Network error: " + str(error))

        try:
            if connected:
                send(sock, {"type": "quit"})
        except OSError:
            pass

    try:
        while True:
            curses.wrapper(interface)
            if not reauth_required:
                break
            try:
                new_password = getpass.getpass("\n  Password changed or rejected. Enter password to reconnect (blank to exit): ")
            except (KeyboardInterrupt, EOFError):
                break
            if not new_password:
                break
            credential[:] = b"\x00" * len(credential)
            credential.clear()
            credential.extend(new_password.encode("utf-8"))
            new_password = ""
            reauth_required = False
            retry_index = 0
            next_reconnect_at = time.monotonic()
            connection_state = "DISCONNECTED"
    except KeyboardInterrupt:
        pass
    finally:
        try:
            sock.close()
        except (AttributeError, OSError):
            pass
        credential[:] = b"\x00" * len(credential)
        credential.clear()
        print("\nJAYMOH CONNECT SESSION CLOSED.")


if __name__ == "__main__":
    main()
