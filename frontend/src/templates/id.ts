// Generate a short, filename-safe workflow id. The id is an internal handle
// (it becomes the on-disk filename); the human-facing label is the workflow
// name, so users never type an id. Six lowercase letters give ~3e8 ids — ample
// for one operator's list; a rare collision surfaces as a 409 and a fresh id is
// drawn on the next submit.
const ALPHABET = "abcdefghijklmnopqrstuvwxyz";
const LENGTH = 6;

export function generateWorkflowId(): string {
  const max = Math.floor(0xffffffff / ALPHABET.length) * ALPHABET.length; // reject-sampling bound for an unbiased pick
  const pick = (): number => {
    const c = globalThis.crypto;
    if (c?.getRandomValues) {
      const buf = new Uint32Array(1);
      do {
        c.getRandomValues(buf);
      } while (buf[0]! >= max);
      return buf[0]! % ALPHABET.length;
    }
    return Math.floor(Math.random() * ALPHABET.length);
  };
  let out = "";
  for (let i = 0; i < LENGTH; i++) out += ALPHABET[pick()];
  return out;
}
