import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  BoardChatComposer,
  clearBoardChatDrafts,
} from "@/components/BoardChatComposer";

const DRAFT_PREFIX = "mc-chat-draft:";
const STORAGE_KEY_A = `${DRAFT_PREFIX}board:b1:chat::userA`;
const STORAGE_KEY_B = `${DRAFT_PREFIX}board:b1:chat::userB`;

type RenderOptions = {
  draftKey?: string | null;
  draftActorId?: string | null;
  onSend?: (content: string) => Promise<boolean>;
};

const renderComposer = ({
  draftKey = "board:b1:chat",
  draftActorId = "userA",
  onSend = vi.fn().mockResolvedValue(true),
}: RenderOptions = {}) =>
  render(
    <BoardChatComposer
      draftKey={draftKey}
      draftActorId={draftActorId}
      onSend={onSend}
    />,
  );

const getTextarea = (): HTMLTextAreaElement =>
  screen.getByRole("textbox") as HTMLTextAreaElement;

const type = (text: string) => {
  fireEvent.change(getTextarea(), { target: { value: text } });
};

const readDraft = (storageKey: string): string | null => {
  const raw = window.localStorage.getItem(storageKey);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as { v: number; t: number; s: string };
    return parsed.s;
  } catch {
    return null;
  }
};

describe("BoardChatComposer unsent drafts (T-107–T-110)", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    window.localStorage.clear();
    vi.restoreAllMocks();
  });

  it("restores the draft after unmount/remount with the same key and actor (T-107)", () => {
    const { unmount } = renderComposer();
    type("draft for board chat");

    expect(readDraft(STORAGE_KEY_A)).toBe("draft for board chat");

    unmount();
    expect(window.localStorage.getItem(STORAGE_KEY_A)).not.toBeNull();

    renderComposer();
    expect(getTextarea().value).toBe("draft for board chat");
  });

  it("keeps A and B actor keys separate: A/B drafts never mix text (T-107/T-108)", () => {
    const first = renderComposer();
    type("actor A text");
    first.unmount();

    const second = renderComposer({ draftActorId: "userB" });
    expect(getTextarea().value).toBe("");
    type("actor B text");
    second.unmount();

    renderComposer();
    expect(getTextarea().value).toBe("actor A text");
  });

  it("hides the previous actor's draft when the actor switches while mounted (T-108)", () => {
    const view = renderComposer();
    type("actor A text");
    expect(getTextarea().value).toBe("actor A text");

    view.rerender(
      <BoardChatComposer
        draftKey="board:b1:chat"
        draftActorId="userB"
        onSend={vi.fn().mockResolvedValue(true)}
      />,
    );
    expect(getTextarea().value).toBe("");
    // Actor A's draft stays stored under A's own key, not visible to B.
    expect(readDraft(STORAGE_KEY_A)).toBe("actor A text");
    expect(window.localStorage.getItem(STORAGE_KEY_B)).toBeNull();
  });

  it("clears only this feature's scoped keys on logout (T-108)", () => {
    window.localStorage.setItem(STORAGE_KEY_A, JSON.stringify({ stub: "a" }));
    window.localStorage.setItem(STORAGE_KEY_B, JSON.stringify({ stub: "b" }));
    window.localStorage.setItem("mc_local_auth_token", "keep-me");
    window.localStorage.setItem("other:feature", "keep-me-too");

    clearBoardChatDrafts();

    expect(window.localStorage.getItem(STORAGE_KEY_A)).toBeNull();
    expect(window.localStorage.getItem(STORAGE_KEY_B)).toBeNull();
    expect(window.localStorage.getItem("mc_local_auth_token")).toBe("keep-me");
    expect(window.localStorage.getItem("other:feature")).toBe("keep-me-too");
  });

  it("preserves text when the send fails by returning false (T-107)", async () => {
    const onSend = vi.fn().mockResolvedValue(false);
    renderComposer({ onSend });

    type("will not send");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
    });

    expect(onSend).toHaveBeenCalledWith("will not send");
    expect(getTextarea().value).toBe("will not send");
    expect(readDraft(STORAGE_KEY_A)).toBe("will not send");
  });

  it("preserves text when onSend throws (T-107)", async () => {
    const onSend = vi.fn().mockRejectedValue(new Error("network down"));
    renderComposer({ onSend });

    type("will throw");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
    });

    expect(getTextarea().value).toBe("will throw");
    expect(readDraft(STORAGE_KEY_A)).toBe("will throw");
  });

  it("retains newer typing after a successful send clears only the submitted revision (T-109)", async () => {
    let resolveSend: ((ok: boolean) => void) | null = null;
    const onSend = vi.fn().mockImplementation(
      () =>
        new Promise<boolean>((resolve) => {
          resolveSend = resolve;
        }),
    );
    renderComposer({ onSend });

    type("first revision");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
    });
    expect(onSend).toHaveBeenCalledWith("first revision");

    // Type while the send result is still pending.
    type("first revision plus newer typing");

    await act(async () => {
      resolveSend?.(true);
    });

    expect(getTextarea().value).toBe("first revision plus newer typing");
    expect(readDraft(STORAGE_KEY_A)).toBe("first revision plus newer typing");
  });

  it("clears the draft when the send succeeds and no newer typing happened (T-109)", async () => {
    const onSend = vi.fn().mockResolvedValue(true);
    renderComposer({ onSend });

    type("send me");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
    });

    expect(getTextarea().value).toBe("");
    expect(window.localStorage.getItem(STORAGE_KEY_A)).toBeNull();
  });

  it("truncates oversized drafts deterministically with a marker (T-110)", () => {
    const first = renderComposer();

    const oversized = "x".repeat(9000);
    type(oversized);

    const stored = readDraft(STORAGE_KEY_A);
    expect(stored).not.toBeNull();
    expect((stored as string).length).toBe(8000);
    expect(stored as string).toMatch(/… \[draft truncated\]$/);
    expect(getTextarea().value).toBe(stored);

    // Deterministic: the same input always truncates to the same output.
    first.unmount();
    const view = renderComposer();
    expect(getTextarea().value).toBe(stored);
    type(oversized);
    expect(readDraft(STORAGE_KEY_A)).toBe(stored);
    view.unmount();
  });

  it("drops expired drafts on read (T-110)", () => {
    const eightDaysAgo = Date.now() - 8 * 24 * 60 * 60 * 1000;
    window.localStorage.setItem(
      STORAGE_KEY_A,
      JSON.stringify({ v: 1, t: eightDaysAgo, s: "stale draft" }),
    );

    renderComposer();

    expect(getTextarea().value).toBe("");
    expect(window.localStorage.getItem(STORAGE_KEY_A)).toBeNull();
  });

  it("keeps a fresh (non-expired) draft on read (T-110)", () => {
    window.localStorage.setItem(
      STORAGE_KEY_A,
      JSON.stringify({ v: 1, t: Date.now() - 1000, s: "fresh draft" }),
    );

    renderComposer();

    expect(getTextarea().value).toBe("fresh draft");
  });
});
