/**
 * Who may reach the key owner's memory.
 *
 * One Membase key is one person's memory. OpenClaw tells a plugin, per turn, whose conversation it
 * is (the host-minted `MemoryAudience`): the trusted owner's own direct conversation
 * (`owner-private`), or a conversation that includes someone else or a group (`conversation`).
 * Membase is read and written only for the owner by default; shared conversations may be allowed
 * to read, never to write.
 */
import type { OpenClawPluginToolContext } from "openclaw/plugin-sdk/plugin-entry";
import type { MembasePluginConfig } from "./config.js";

export type MemoryAudience = NonNullable<OpenClawPluginToolContext["memoryAudience"]>;
export type Access = "read" | "write";
type PolicyConfig = Pick<MembasePluginConfig, "allowSharedConversations">;

/** The host's audience decides; no audience grants nothing (the memory-provider contract). */
export function audienceAllows(
  audience: MemoryAudience | undefined,
  access: Access,
  config: PolicyConfig,
  agentId?: string,
): boolean {
  if (!audience) return false;
  if (agentId && audience.agentId !== agentId) return false;
  if (audience.kind === "owner-private") return true;
  return access === "read" && config.allowSharedConversations;
}

/**
 * Whether a tool is offered on this turn. With an audience, the audience decides. Without one
 * (OpenClaw resolves it only on some paths, e.g. not for Gateway `/tools/invoke` calls), the
 * host's trusted owner bit decides: only the owner reaches their own memory.
 */
export function toolAllowed(ctx: OpenClawPluginToolContext, access: Access, config: PolicyConfig): boolean {
  if (ctx.memoryAudience) return audienceAllows(ctx.memoryAudience, access, config, ctx.agentId);
  return ctx.senderIsOwner === true;
}
