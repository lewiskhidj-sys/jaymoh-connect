import asyncio
import json
import sys

PORT = 8765

async def send(writer, data):
    writer.write((json.dumps(data) + "\n").encode())
    await writer.drain()

async def main():
    if len(sys.argv) < 3:
        print("Usage: python client/connection.py IP USERNAME")
        return

    host = sys.argv[1]
    username = sys.argv[2]

    print("=" * 45)
    print("       JAYMOH CONNECT CLIENT")
    print("=" * 45)
    print(f"Connecting to {host}:{PORT}...")

    try:
        reader, writer = await asyncio.open_connection(host, PORT)

        await send(writer, {
            "type": "hello",
            "username": username
        })

        response = json.loads(
            (await asyncio.wait_for(
                reader.readline(), timeout=15
            )).decode()
        )

        if response.get("type") != "welcome":
            print("Connection rejected:", response)
            writer.close()
            await writer.wait_closed()
            return

        print("\nCONNECTION ESTABLISHED")
        print("Server :", response["server"])
        print("User   :", "@" + response["username"])
        print("Version:", response["version"])

        await send(writer, {"type": "ping"})
        pong = json.loads((await reader.readline()).decode())
        print("PING   :", pong.get("type", "unknown"))

        await send(writer, {"type": "quit"})
        await reader.readline()

        writer.close()
        await writer.wait_closed()

        print("\nDisconnected successfully.")

    except ConnectionRefusedError:
        print("ERROR: Server refused the connection.")
    except (OSError, asyncio.TimeoutError) as error:
        print("CONNECTION ERROR:", error)

if __name__ == "__main__":
    asyncio.run(main())
