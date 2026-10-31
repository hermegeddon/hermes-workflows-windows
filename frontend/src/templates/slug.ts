// The id charset the core enforces (an id becomes the on-disk filename). Mirrored
// client-side so an obviously-bad id is rejected before a round-trip; the core
// stays the authority and re-validates on create.
const SLUG = /^[A-Za-z0-9_-]+$/;

export function isValidSlug(id: string): boolean {
  return SLUG.test(id);
}
