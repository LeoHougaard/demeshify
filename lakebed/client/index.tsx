export function App() {
  return (
    <main className="grid min-h-screen place-items-center bg-neutral-950 px-6 text-neutral-100">
      <section className="w-full max-w-sm">
        <h1 className="text-3xl font-semibold tracking-tight">Demeshify</h1>
        <p className="mt-3 text-neutral-300">Convert STL to STEP in your browser. Your models stay on your device.</p>
        <a
          className="mt-6 inline-block rounded-md bg-neutral-200 px-4 py-2 font-medium text-neutral-950 hover:bg-white"
          href="https://leohougaard.github.io/demeshify/"
          referrerPolicy="no-referrer"
        >
          Open converter
        </a>
      </section>
    </main>
  );
}
