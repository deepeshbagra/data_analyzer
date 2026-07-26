export default function Home() {
  return (
    <main className="mx-auto max-w-2xl px-6 py-16">
      <h1 className="text-2xl font-semibold tracking-tight">Document Intelligence</h1>
      <p className="mt-3 text-neutral-600 dark:text-neutral-400">
        Foundation only. Feature routes arrive with their phases:
      </p>
      <ul className="mt-6 space-y-2 text-sm">
        <li>
          <code className="rounded bg-neutral-100 px-1.5 py-0.5 dark:bg-neutral-800">
            /review
          </code>{" "}
          extraction review — Phase 1
        </li>
        <li>
          <code className="rounded bg-neutral-100 px-1.5 py-0.5 dark:bg-neutral-800">
            /reconcile
          </code>{" "}
          proposed links — Phase 2
        </li>
        <li>
          <code className="rounded bg-neutral-100 px-1.5 py-0.5 dark:bg-neutral-800">
            /dashboard
          </code>{" "}
          KPIs and findings inbox — Phase 3
        </li>
      </ul>
    </main>
  );
}
