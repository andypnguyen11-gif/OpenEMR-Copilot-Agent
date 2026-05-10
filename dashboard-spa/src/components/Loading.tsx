// Mirrors templates/patient/card/loader.html.twig — Bootstrap 4.6 spinner
// with an sr-only label so card loading states stay accessible.
export function Loading({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="text-muted ml-2">
      <div className="spinner-border spinner-border-sm" role="status">
        <span className="sr-only">{label}…</span>
      </div>
    </div>
  )
}
