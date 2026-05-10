// Empty-state variants used across the six clinical cards. The Allergies
// card needs the NKDA distinction (a coded "no known allergies" record vs
// no record at all); other cards collapse to "Nothing Recorded".
export type EmptyVariant = 'no-known-allergies' | 'nothing-recorded'

const MESSAGES: Record<EmptyVariant, string> = {
  'no-known-allergies': 'No Known Allergies',
  'nothing-recorded': 'Nothing Recorded',
}

export function EmptyState({ variant }: { variant: EmptyVariant }) {
  return <p className="text-muted mb-0 ml-2">{MESSAGES[variant]}</p>
}
