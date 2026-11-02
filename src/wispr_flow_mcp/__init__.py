def main() -> None:
    # Imported here so the archive commands do not load the MCP library they never use.
    from .server import main as server_main

    server_main()


__all__ = ["main"]
