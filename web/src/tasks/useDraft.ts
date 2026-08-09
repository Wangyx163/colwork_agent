import { useEffect, useState } from "react";

/** A form field that survives a reload.
 *
 *  Submitting a deliverable means typing something long into a box on a page
 *  that refreshes itself after every action. Losing that to a stray reload is
 *  the kind of small betrayal that stops people using a tool, so the text is
 *  kept in this browser until it has been sent.
 *
 *  Scoped per task and per field: two tasks open in two tabs must not write
 *  over each other. */
export function useDraft(
  key: string,
  initial = "",
): [string, (value: string) => void, () => void] {
  const storageKey = `collabDraft:${key}`;
  const [value, setValue] = useState(
    () => localStorage.getItem(storageKey) ?? initial,
  );

  useEffect(() => {
    if (value) localStorage.setItem(storageKey, value);
    else localStorage.removeItem(storageKey);
  }, [storageKey, value]);

  const clear = () => {
    localStorage.removeItem(storageKey);
    setValue(initial);
  };

  return [value, setValue, clear];
}
