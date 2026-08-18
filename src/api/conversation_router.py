"""Route task conversations on one persistent browser page."""

from __future__ import annotations

from src.provider_errors import ProviderError


class ConversationRouteError(ProviderError):
    code = "CONVERSATION_ROUTE_FAILED"
    error_type = "conversation_route_failed"
    status_code = 409
    retryable = False


class ConversationRouter:
    async def verify(self, client, provider_thread_id: str | None) -> None:
        """Refuse to send unless the active provider conversation is exact."""
        if not provider_thread_id:
            return
        verifier = getattr(client, "verify_current_conversation", None)
        if verifier is not None:
            verified = await verifier(provider_thread_id)
            if verified:
                return
        elif client._extract_thread_id() == provider_thread_id:
            return
        raise ConversationRouteError(
            f"Active provider conversation does not match {provider_thread_id}"
        )

    async def prepare(
        self,
        client,
        *,
        task_id: str | None,
        provider_thread_id: str | None,
        provider_thread_url: str | None = None,
    ) -> None:
        if not provider_thread_id:
            await client.new_chat()
            return

        switcher = getattr(client, "switch_conversation", None)
        if switcher is not None:
            try:
                await switcher(
                    provider_thread_id,
                    title=task_id,
                    conversation_url=provider_thread_url,
                )
                await self.verify(client, provider_thread_id)
                return
            except Exception as error:
                raise ConversationRouteError(
                    f"Could not switch to provider conversation {provider_thread_id}"
                ) from error

        # Compatibility path for providers that do not expose the richer
        # switch API yet: Recent-list first, direct URL navigation second.
        opener = getattr(client, "open_thread_by_title", None)
        if task_id and opener is not None:
            try:
                if await opener(task_id, provider_thread_id):
                    await self.verify(client, provider_thread_id)
                    return
            except Exception:
                pass
        try:
            if client._extract_thread_id() != provider_thread_id:
                await client.navigate_to_thread(provider_thread_id)
            await self.verify(client, provider_thread_id)
        except Exception as error:
            raise ConversationRouteError(
                f"Task conversation {task_id or provider_thread_id} was not found or verification failed"
            ) from error

    async def bind_created(self, client, *, task_id: str | None, provider_thread_id: str) -> None:
        if not provider_thread_id:
            raise ConversationRouteError("Provider did not expose a conversation ID after creation")
        await self.verify(client, provider_thread_id)
        if not task_id:
            return
        renamer = getattr(client, "rename_current_conversation", None)
        if renamer is None or not await renamer(task_id):
            raise ConversationRouteError(
                f"Could not rename provider conversation {provider_thread_id} to {task_id}"
            )
        await self.verify(client, provider_thread_id)
