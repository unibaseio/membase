"""Media memory: photos, videos and audio with their own routes, search and ask, apart from
documents and memories. Local for now; the engine is ``membase_media`` (the ``[media]`` extra).

Everything media lives in this package; the rest of the SDK only mounts it: ``client.py``
(``Membase.media``), ``local/backend.py`` (``LocalBackend.media``), ``local/routes.py`` (the
``/v1/media`` routes), ``cli.py`` (``membase media``) and ``mcp/`` (the media tools)."""
