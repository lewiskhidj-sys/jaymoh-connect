# JAYMOH CONNECT

A Python terminal-based messaging application with private chats, group conversations, delivery and read receipts, member management, and online presence.

## Features

- Private messaging
- Group creation and messaging
- Group member management
- Online/offline presence
- Delivery and read receipts
- Automatic reconnection
- SQLite storage
- Terminal-based interface

## Requirements

- Python 3.10 or newer
- No third-party Python packages required

## Installation

Clone the repository:

    git clone YOUR_REPOSITORY_URL
    cd jaymoh-connect

## Run locally

Open a Termux terminal and start the server:

    python server/main.py

Open another terminal in the same project directory and start the client:

    python client/main.py

For a remote server:

    python client/main.py SERVER_IP

The server uses TCP port 8765.

## Database

The server stores account and message data in:

    data/connect.db

Keep this database private. Do not upload it to GitHub.

## Security notice

This is an early development release. Client-server communication currently uses unencrypted TCP. Do not use it to transmit sensitive information over untrusted networks or expose the server directly to the public internet.

## License

MIT License. See LICENSE.
