"""Development echo MCP server (S06): a dependency-free target for autostart and smoke tests.

Not a data system: it reads and writes nothing outside its own process. Its tools are absent from
``config/tool_permissions.yaml``, so the platform treats them as UNCLASSIFIED unless a test
registry classifies them.
"""
