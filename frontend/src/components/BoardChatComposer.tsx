"use client";

import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

const MENTION_MAX_OPTIONS = 8;
const MENTION_PATTERN = /(?:^|\s)@([A-Za-z0-9_-]{0,31})$/;

type MentionTarget = {
  start: number;
  end: number;
  query: string;
};

type BoardChatComposerProps = {
  placeholder?: string;
  isSending?: boolean;
  disabled?: boolean;
  mentionSuggestions?: string[];
  /**
   * T-107/T-108: draft persistence scope. When both `draftKey` and
   * `draftActorId` are non-empty strings, the composer text is persisted in
   * localStorage under this feature's prefix so navigating away and back
   * (same board/task + actor) restores the draft. When either is missing,
   * draft persistence is disabled entirely.
   */
  draftKey?: string | null;
  /**
   * Current actor identity (e.g. Clerk user id or local-auth "local-user").
   * Drafts are keyed by actor so two actors' texts never mix.
   */
  draftActorId?: string | null;
  onSend: (content: string) => Promise<boolean>;
};

/*
 * T-107/T-108/T-110 — unsent draft storage scope (board chat only):
 * - Storage: browser localStorage, this feature only. Never a broad
 *   localStorage.clear(); removal is limited to keys with this prefix.
 * - Key format: `${DRAFT_STORAGE_PREFIX}${draftKey}::${draftActorId}` where
 *   draftKey identifies the board/task surface (e.g. "board:<id>:chat",
 *   "board:<id>:task:<taskId>", "group:<id>:chat", "group:<id>:notes") and
 *   draftActorId is the signed-in user's id, so A/B keys never mix text.
 * - Value: JSON `{ v: 1, t: <savedAt epoch ms>, s: <text> }` — text only;
 *   mention-menu state is never persisted.
 * - Lifetime: bounded TTL (DRAFT_TTL_MS). Expired drafts are dropped on read.
 * - Size cap: DRAFT_MAX_CHARS with a deterministic truncation marker.
 * - Logout: pages call clearBoardChatDrafts() on sign-out, which removes only
 *   this feature's prefixed keys.
 * - Deferred dependency: general-chat (session-scoped) draft keying is NOT
 *   implemented here; it waits on group 2's T-015 chat ownership decision.
 * - Explicitly rejected: no background queue, no send replay/retry, no
 *   offline-send behavior.
 */
const DRAFT_STORAGE_PREFIX = "mc-chat-draft:";
const DRAFT_MAX_CHARS = 8000;
const DRAFT_TRUNCATION_MARKER = "… [draft truncated]";
const DRAFT_TTL_MS = 7 * 24 * 60 * 60 * 1000;

type StoredDraft = { v: 1; t: number; s: string };

const draftStorageKey = (draftKey: string, actorId: string): string =>
  `${DRAFT_STORAGE_PREFIX}${draftKey}::${actorId}`;

/** T-110: deterministic cap — oversized input is sliced and marked. */
const enforceDraftCap = (text: string): string => {
  if (text.length <= DRAFT_MAX_CHARS) return text;
  return (
    text.slice(0, DRAFT_MAX_CHARS - DRAFT_TRUNCATION_MARKER.length) +
    DRAFT_TRUNCATION_MARKER
  );
};

const removeStoredDraft = (storageKey: string): void => {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(storageKey);
  } catch {
    // Ignore storage failures (private mode / policy).
  }
};

/** T-110: TTL-bounded read; expired or malformed drafts are dropped. */
const readStoredDraft = (storageKey: string): string | null => {
  if (typeof window === "undefined") return null;
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(storageKey);
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as StoredDraft;
    if (
      !parsed ||
      typeof parsed !== "object" ||
      parsed.v !== 1 ||
      typeof parsed.t !== "number" ||
      typeof parsed.s !== "string"
    ) {
      removeStoredDraft(storageKey);
      return null;
    }
    if (Date.now() - parsed.t > DRAFT_TTL_MS) {
      removeStoredDraft(storageKey);
      return null;
    }
    return enforceDraftCap(parsed.s);
  } catch {
    removeStoredDraft(storageKey);
    return null;
  }
};

const writeStoredDraft = (storageKey: string, text: string): void => {
  if (typeof window === "undefined") return;
  if (!text) {
    removeStoredDraft(storageKey);
    return;
  }
  try {
    const draft: StoredDraft = { v: 1, t: Date.now(), s: text };
    window.localStorage.setItem(storageKey, JSON.stringify(draft));
  } catch {
    // Ignore storage failures (private mode / quota / policy).
  }
};

/**
 * T-108: clears only this feature's scoped draft keys (never a broad
 * localStorage.clear()). Called by consumer pages on sign-out/logout.
 */
export function clearBoardChatDrafts(): void {
  if (typeof window === "undefined") return;
  try {
    const keys: string[] = [];
    for (let index = 0; index < window.localStorage.length; index += 1) {
      const key = window.localStorage.key(index);
      if (key && key.startsWith(DRAFT_STORAGE_PREFIX)) keys.push(key);
    }
    keys.forEach((key) => window.localStorage.removeItem(key));
  } catch {
    // Ignore storage failures (private mode / policy).
  }
}

const subscribeToNothing = (): (() => void) => () => {};
const getIsClientSnapshot = (): boolean => true;
const getIsServerSnapshot = (): boolean => false;

const normalizeMentionHandle = (raw: string): string | null => {
  const trimmed = raw.trim().replace(/^@+/, "");
  if (!trimmed) return null;
  const token = trimmed.split(/\s+/)[0]?.replace(/[^A-Za-z0-9_-]/g, "") ?? "";
  if (!token) return null;
  if (!/^[A-Za-z]/.test(token)) return null;
  return token.slice(0, 32).toLowerCase();
};

const findMentionTarget = (
  text: string,
  caret: number,
): MentionTarget | null => {
  if (caret < 0 || caret > text.length) return null;
  const prefix = text.slice(0, caret);
  const match = prefix.match(MENTION_PATTERN);
  if (!match) return null;
  const query = (match[1] ?? "").toLowerCase();
  const start = caret - query.length - 1;
  return { start, end: caret, query };
};

function BoardChatComposerImpl({
  placeholder = "Message the board lead. Tag agents with @name.",
  isSending = false,
  disabled = false,
  mentionSuggestions,
  draftKey = null,
  draftActorId = null,
  onSend,
}: BoardChatComposerProps) {
  // T-107/T-108: actor- and scope-keyed draft storage (see scope comment at
  // the DRAFT_* constants above). Disabled entirely when either part is
  // missing so the composer also works without draft persistence.
  const draftsEnabled = Boolean(draftKey && draftActorId);
  const storageKey = draftsEnabled
    ? draftStorageKey(draftKey as string, draftActorId as string)
    : null;
  // SSR-safe client flag: during server render and hydration the server
  // snapshot (false) is used, so draft reads never cause hydration mismatches.
  const isClient = useSyncExternalStore(
    subscribeToNothing,
    getIsClientSnapshot,
    getIsServerSnapshot,
  );
  const effectiveStorageKey = isClient ? storageKey : null;
  const [value, setValue] = useState("");
  const [mentionTarget, setMentionTarget] = useState<MentionTarget | null>(
    null,
  );
  const [activeMentionIndex, setActiveMentionIndex] = useState(0);
  // T-107/T-108: hydrate the draft for the current board/task + actor scope.
  // The state is re-adjusted during render whenever the scope or actor
  // identity changes (the React-documented "adjust state when a prop
  // changes" pattern), so a switched actor never sees the previous actor's
  // draft (keys never mix) and navigating away and back within the same
  // scope restores the draft.
  const [hydratedStorageKey, setHydratedStorageKey] = useState<string | null>(
    null,
  );
  if (hydratedStorageKey !== effectiveStorageKey) {
    setHydratedStorageKey(effectiveStorageKey);
    const stored = effectiveStorageKey
      ? (readStoredDraft(effectiveStorageKey) ?? "")
      : "";
    setValue(stored);
    setMentionTarget(null);
    setActiveMentionIndex(0);
  }
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const closeMenuTimeoutRef = useRef<number | null>(null);
  const shouldFocusAfterSendRef = useRef(false);

  const updateValue = useCallback(
    (next: string) => {
      const capped = enforceDraftCap(next);
      setValue(capped);
      if (effectiveStorageKey) {
        writeStoredDraft(effectiveStorageKey, capped);
      }
    },
    [effectiveStorageKey],
  );

  const mentionOptions = useMemo(() => {
    const handles = new Set<string>(["lead"]);
    (mentionSuggestions ?? []).forEach((candidate) => {
      const handle = normalizeMentionHandle(candidate);
      if (handle) {
        handles.add(handle);
      }
    });
    return [...handles];
  }, [mentionSuggestions]);

  const filteredMentionOptions = useMemo(() => {
    if (!mentionTarget) return [];
    const query = mentionTarget.query;
    const startsWithMatches = mentionOptions.filter((option) =>
      option.startsWith(query),
    );
    return startsWithMatches.slice(0, MENTION_MAX_OPTIONS);
  }, [mentionOptions, mentionTarget]);

  const activeIndex =
    filteredMentionOptions.length > 0
      ? Math.min(activeMentionIndex, filteredMentionOptions.length - 1)
      : 0;

  useEffect(() => {
    if (isSending) return;
    if (!shouldFocusAfterSendRef.current) return;
    shouldFocusAfterSendRef.current = false;
    textareaRef.current?.focus();
  }, [isSending]);

  useEffect(() => {
    return () => {
      if (closeMenuTimeoutRef.current !== null) {
        window.clearTimeout(closeMenuTimeoutRef.current);
      }
    };
  }, []);

  const refreshMentionTarget = useCallback(
    (nextValue: string, caret: number) => {
      const nextTarget = findMentionTarget(nextValue, caret);
      setMentionTarget(nextTarget);
    },
    [],
  );

  const applyMentionSelection = useCallback(
    (handle: string) => {
      const textarea = textareaRef.current;
      if (!textarea || !mentionTarget) return;
      const replacement = `@${handle} `;
      const nextValue =
        value.slice(0, mentionTarget.start) +
        replacement +
        value.slice(mentionTarget.end);
      updateValue(nextValue);
      setMentionTarget(null);
      setActiveMentionIndex(0);
      window.requestAnimationFrame(() => {
        const nextCaret = mentionTarget.start + replacement.length;
        textarea.focus();
        textarea.setSelectionRange(nextCaret, nextCaret);
      });
    },
    [mentionTarget, updateValue, value],
  );

  const send = useCallback(async () => {
    if (isSending || disabled) return;
    // T-109: capture the exact revision being submitted so only that
    // revision is cleared on success; anything typed while awaiting the
    // send result survives.
    const submittedValue = value;
    const trimmed = submittedValue.trim();
    if (!trimmed) return;
    let ok = false;
    try {
      ok = await onSend(trimmed);
    } catch {
      // T-107: a failed (throwing) send preserves the composer text.
      ok = false;
    }
    shouldFocusAfterSendRef.current = true;
    if (!ok) return;
    if (storageKey) {
      // Remove only the stored value that matches the submitted revision;
      // if newer typing was persisted while awaiting, it stays.
      const persisted = readStoredDraft(storageKey);
      if (persisted === submittedValue) {
        removeStoredDraft(storageKey);
      }
    }
    setValue((prev) => (prev === submittedValue ? "" : prev));
    setMentionTarget(null);
    setActiveMentionIndex(0);
  }, [disabled, isSending, onSend, storageKey, value]);

  return (
    <div className="mt-4 space-y-2">
      <div className="relative">
        <Textarea
          ref={textareaRef}
          value={value}
          onChange={(event) => {
            const nextValue = event.target.value;
            updateValue(nextValue);
            refreshMentionTarget(
              nextValue,
              event.target.selectionStart ?? nextValue.length,
            );
          }}
          onClick={(event) => {
            refreshMentionTarget(
              value,
              event.currentTarget.selectionStart ?? value.length,
            );
          }}
          onKeyUp={(event) => {
            refreshMentionTarget(
              value,
              event.currentTarget.selectionStart ?? value.length,
            );
          }}
          onBlur={() => {
            if (closeMenuTimeoutRef.current !== null) {
              window.clearTimeout(closeMenuTimeoutRef.current);
            }
            closeMenuTimeoutRef.current = window.setTimeout(() => {
              setMentionTarget(null);
              setActiveMentionIndex(0);
            }, 120);
          }}
          onFocus={(event) => {
            refreshMentionTarget(
              value,
              event.currentTarget.selectionStart ?? value.length,
            );
          }}
          onKeyDown={(event) => {
            if (filteredMentionOptions.length > 0 && mentionTarget) {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setActiveMentionIndex(
                  (prev) => (prev + 1) % filteredMentionOptions.length,
                );
                return;
              }
              if (event.key === "ArrowUp") {
                event.preventDefault();
                setActiveMentionIndex(
                  (prev) =>
                    (prev - 1 + filteredMentionOptions.length) %
                    filteredMentionOptions.length,
                );
                return;
              }
              if (event.key === "Enter" || event.key === "Tab") {
                event.preventDefault();
                const selected = filteredMentionOptions[activeIndex];
                if (selected) {
                  applyMentionSelection(selected);
                }
                return;
              }
              if (event.key === "Escape") {
                event.preventDefault();
                setMentionTarget(null);
                setActiveMentionIndex(0);
                return;
              }
            }
            if (event.key !== "Enter") return;
            if (event.nativeEvent.isComposing) return;
            if (event.shiftKey) return;
            event.preventDefault();
            void send();
          }}
          placeholder={placeholder}
          className="min-h-[120px]"
          disabled={isSending || disabled}
        />
        {mentionTarget && filteredMentionOptions.length > 0 ? (
          <div className="absolute bottom-full left-0 z-20 mb-2 w-full overflow-hidden rounded-xl border border-slate-200 bg-white shadow-lg">
            <div className="max-h-52 overflow-y-auto py-1">
              {filteredMentionOptions.map((option, index) => (
                <button
                  key={option}
                  type="button"
                  onMouseDown={(event) => {
                    event.preventDefault();
                    applyMentionSelection(option);
                  }}
                  className={`flex w-full items-center justify-between px-3 py-2 text-left text-sm transition ${
                    index === activeIndex
                      ? "bg-slate-100 text-slate-900"
                      : "text-slate-700 hover:bg-slate-50"
                  }`}
                >
                  <span className="font-mono">@{option}</span>
                  <span className="text-xs text-slate-400">mention</span>
                </button>
              ))}
            </div>
          </div>
        ) : null}
      </div>
      <div className="flex justify-end">
        <Button
          onClick={() => void send()}
          disabled={isSending || disabled || !value.trim()}
        >
          {isSending ? "Sending…" : "Send"}
        </Button>
      </div>
    </div>
  );
}

export const BoardChatComposer = memo(BoardChatComposerImpl);
BoardChatComposer.displayName = "BoardChatComposer";
