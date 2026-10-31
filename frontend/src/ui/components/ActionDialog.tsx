import { useEffect, useRef, useState } from "react";
import { Button } from "./Button";
import { Field } from "./Field";
import { Input } from "./Input";
import { Modal } from "./Modal";

export interface ActionDialogProps {
  title: string;
  description: string;
  submitLabel: string;
  danger?: boolean;
  fieldLabel?: string;
  initialValue?: string;
  validate?: (value: string) => string | null;
  onSubmit: (value: string) => Promise<unknown>;
  onSuccess: () => void;
  onClose: () => void;
}

/** DOM dialog for embedded browsers that do not implement prompt/confirm. */
export function ActionDialog(props: ActionDialogProps): React.ReactElement {
  const [value, setValue] = useState(props.initialValue ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submitting = useRef(false);
  const form = useRef<HTMLFormElement>(null);
  const close = () => { if (!submitting.current) props.onClose(); };

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const dialog = form.current?.closest('[role="dialog"]');
    (dialog?.querySelector("input, button[data-cancel]") as HTMLElement | null)?.focus();
    const trapFocus = (event: KeyboardEvent) => {
      if (event.key !== "Tab" || !dialog) return;
      const controls = Array.from(dialog.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled)"));
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (!controls.length || !dialog.contains(document.activeElement)) {
        event.preventDefault();
        (first ?? dialog as HTMLElement).focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault(); last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first?.focus();
      }
    };
    document.addEventListener("keydown", trapFocus);
    return () => {
      document.removeEventListener("keydown", trapFocus);
      if (previous?.isConnected) previous.focus();
    };
  }, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    const text = value.trim();
    const problem = props.validate?.(text);
    if (problem) { setError(problem); return; }
    submitting.current = true;
    setBusy(true); setError(null);
    try {
      await props.onSubmit(text);
      props.onSuccess();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "The action failed. Please try again.");
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }

  return <Modal title={props.title} onClose={close} dismissDisabled={busy}>
    <form ref={form} onSubmit={submit} aria-busy={busy} className="hw-form">
      <p>{props.description}</p>
      {props.fieldLabel && <Field label={props.fieldLabel}>
        <Input value={value} disabled={busy} onChange={(event) => setValue(event.target.value)} />
      </Field>}
      {error && <p role="alert" className="hw-alert">{error}</p>}
      {busy && <p role="status">Saving…</p>}
      <div className="hw-actions">
        <Button data-cancel disabled={busy} onClick={close}>Cancel</Button>
        <Button type="submit" variant={props.danger ? "danger" : "primary"} disabled={busy}>{props.submitLabel}</Button>
      </div>
    </form>
  </Modal>;
}
