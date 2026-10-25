"""MembaseRetriever: a Membase search as LangChain ``Document``s."""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForRetrieverRun,
    CallbackManagerForRetrieverRun,
)
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import ensure_config, run_in_executor
from membase.errors import PermissionDeniedError
from pydantic import Field

from ._common import MembaseClientMixin, logger, unavailable, user_id_from


class MembaseUnavailableError(RuntimeError):
    """A search found nothing *and* some Memories could not answer: not evidence of no memory."""


class MembaseRetriever(MembaseClientMixin, BaseRetriever):
    """Passages from the user's Membase memory, most relevant first.

    Each ``Document`` carries ``container_name`` (the Memory it came from) and ``container``
    (its id) in ``metadata``, so an answer can cite where it came from.

    Setup:
        ``pip install langchain-membase`` and set ``MEMBASE_API_KEY`` (Connect > Developer keys
        in the Membase app), or pass ``client=Membase(...)``.

    Instantiate:
        .. code-block:: python

            from langchain_membase import MembaseRetriever

            retriever = MembaseRetriever(k=5)

    Usage:
        .. code-block:: python

            docs = retriever.invoke("what did we decide about the ledger")
            docs[0].metadata["container_name"]  # "Project decisions"

    Many users:
        .. code-block:: python

            retriever = MembaseRetriever(client_for=lambda user_id: Membase(api_key=keys[user_id]))
            retriever.invoke("...", config={"configurable": {"user_id": "alice"}})
    """

    k: int | None = Field(default=None, ge=1, le=50)
    """How many passages in all. Unset lets Membase cover every Memory in reach (at least 8)."""
    container: str | None = None
    """One container (Memory) id to search; unset searches every Memory the key reaches."""

    def invoke(
        self, input: str, config: RunnableConfig | None = None, **kwargs: Any
    ) -> list[Document]:
        config = ensure_config(config)
        if kwargs.get("user_id") is None:
            kwargs["user_id"] = user_id_from(config)
        return super().invoke(input, config, **kwargs)

    async def ainvoke(
        self, input: str, config: RunnableConfig | None = None, **kwargs: Any
    ) -> list[Document]:
        config = ensure_config(config)
        if kwargs.get("user_id") is None:
            kwargs["user_id"] = user_id_from(config)
        return await super().ainvoke(input, config, **kwargs)

    def _get_relevant_documents(
        self,
        query: str,
        *,
        run_manager: CallbackManagerForRetrieverRun,
        k: int | None = None,
        container: str | None = None,
        user_id: str | None = None,
    ) -> list[Document]:
        client = self._membase(user_id)
        try:
            resp = client.search(query, container=container or self.container, limit=k or self.k)
        except PermissionDeniedError as e:
            raise PermissionDeniedError(
                f"{e.message} (this Membase key was narrowed or revoked by its owner; "
                "do not retry with it)",
                status=e.status,
                body=e.body,
            ) from e
        docs = [_document(r) for r in resp.get("results") or [] if r.get("content")]
        failed = unavailable(resp)
        if failed and not docs:
            raise MembaseUnavailableError(
                "no passages came back and these Memories could not answer: " + "; ".join(failed)
            )
        if failed:
            logger.warning("Membase search: some Memories could not answer: %s", "; ".join(failed))
        return docs

    async def _aget_relevant_documents(
        self,
        query: str,
        *,
        run_manager: AsyncCallbackManagerForRetrieverRun,
        k: int | None = None,
        container: str | None = None,
        user_id: str | None = None,
    ) -> list[Document]:
        # The SDK is synchronous; keep the event loop free while it waits on the network.
        return await run_in_executor(
            None,
            self._get_relevant_documents,
            query,
            run_manager=run_manager.get_sync(),
            k=k,
            container=container,
            user_id=user_id,
        )


def _document(r: dict[str, Any]) -> Document:
    metadata = {key: value for key, value in r.items() if key != "content"}
    metadata.setdefault("container_name", r.get("container_name") or r.get("container"))
    return Document(page_content=str(r["content"]), metadata=metadata)
