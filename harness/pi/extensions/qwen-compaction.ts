import { compact, convertToLlm, serializeConversation, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

function isQwen(model: { provider: string; id: string } | undefined) {
  return model && ["omniroute-api", "openclaw-infer"].includes(model.provider)
    && ["vllm/Qwen3.8-27B-FP8", "Qwen3.8-27B-FP8"].includes(model.id);
}

export default function (pi: ExtensionAPI) {
  pi.on("tool_result", async (event, ctx) => {
    if (!isQwen(ctx.model)) return;
    const text = event.content.filter(block => block.type === "text").map(block => block.text).join("\n");
    if (text.length <= 8000) return;
    const path = join(await mkdtemp(join(tmpdir(), "pi-qwen-tool-")), "output.txt");
    await writeFile(path, text, { mode: 0o600 });
    // ponytail: 8000-character inline ceiling; retrieve narrower slices from the saved file.
    return { content: [{ type: "text", text: `${text.slice(0, 8000)}\n[Truncated ${text.length} characters; full output: ${path}. Read a narrow slice rather than the whole file.]` }, ...event.content.filter(block => block.type !== "text")] };
  });
  pi.on("session_before_compact", async (event, ctx) => {
    try {
      const model = ctx.modelRegistry.find("omniroute-api", "vllm/Qwen3.8-27B-FP8");
      if (!model) throw new Error("Local compaction model unavailable");
      // Pi skips cumulative file lists from hook-generated checkpoints; carry ours forward.
      const previous = [...event.branchEntries].reverse().find(entry => entry.type === "compaction");
      const details = previous?.type === "compaction" ? previous.details as { qwenCompaction?: boolean; readFiles?: string[]; modifiedFiles?: string[] } | undefined : undefined;
      if (details?.qwenCompaction) {
        for (const path of details.readFiles ?? []) event.preparation.fileOps.read.add(path);
        for (const path of details.modifiedFiles ?? []) event.preparation.fileOps.edited.add(path);
      }
      const preparation = event.preparation;
      const text = [preparation.previousSummary ?? "", serializeConversation(convertToLlm([
        ...preparation.messagesToSummarize, ...preparation.turnPrefixMessages,
      ]))].join("\n\n");
      // ponytail: serial 6000-codepoint chunks for the 65k local context; no premium fallback.
      const chunks = text.match(/[\s\S]{1,6000}/gu) ?? ["No conversation entries."];
      const usage = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
      let result: Awaited<ReturnType<typeof compact>> | undefined;
      for (const chunk of chunks) {
        if (event.signal.aborted) throw new Error("Compaction cancelled");
        result = await compact({ ...preparation, previousSummary: result?.summary,
          messagesToSummarize: [{ role: "user", content: chunk, timestamp: Date.now() }],
          turnPrefixMessages: [], isSplitTurn: false,
          settings: { ...preparation.settings, reserveTokens: 5120 },
        }, model, undefined, undefined, event.customInstructions, event.signal, "off",
        async (target, context, options) => {
          if (Buffer.byteLength(JSON.stringify(context)) > 48000) throw new Error("Local compaction input too large");
          const stream = await ctx.modelRegistry.streamSimple(target, context, options);
          const response = await stream.result();
          if (event.signal.aborted || !response.content.some(block => block.type === "text" && block.text.trim())) {
            throw new Error("Local compaction returned no complete text");
          }
          return stream;
        });
        if (result.usage) {
          for (const key of ["input", "output", "cacheRead", "cacheWrite", "totalTokens"] as const) usage[key] += result.usage[key];
          for (const key of ["input", "output", "cacheRead", "cacheWrite", "total"] as const) usage.cost[key] += result.usage.cost[key];
        }
      }
      return { compaction: { ...result!, usage, details: { ...(result!.details as object), qwenCompaction: true, chunks: chunks.length } } };
    } catch {
      ctx.ui.notify("Local compaction stopped; history unchanged and no premium fallback.", "error");
      return { cancel: true };
    }
  });
}
