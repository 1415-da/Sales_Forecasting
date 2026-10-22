import { ArrowRight, LineChart, ShieldCheck, Sparkles } from 'lucide-react';
import Link from 'next/link';

import { buttonVariants } from '@/components/ui/button';
import { cn } from '@/lib/utils';

const capabilities = [
  {
    title: 'Plan with confidence',
    description: 'Turn historical sales into clear, horizon-based forecasts for every decision cycle.',
    Icon: LineChart,
  },
  {
    title: 'Test the impact',
    description: 'Compare promotions, closures, price changes, and other scenarios before acting.',
    Icon: Sparkles,
  },
  {
    title: 'Operate with control',
    description: 'Track model health, forecast accuracy, data freshness, and production readiness.',
    Icon: ShieldCheck,
  },
] as const;

/** Public entry point for the Sales Forecasting Platform. */
export default function HomePage() {
  return (
    <main className="relative isolate min-h-screen overflow-hidden px-4 py-6 sm:px-6 lg:px-8">
      <div
        aria-hidden="true"
        className="absolute inset-x-0 top-0 -z-10 h-[32rem] bg-[radial-gradient(ellipse_at_top,_hsl(var(--chart-scenario)/0.16),_transparent_62%)]"
      />

      <div className="mx-auto flex min-h-[calc(100vh-3rem)] max-w-6xl flex-col">
        <header className="flex items-center justify-between py-3 sm:py-5">
          <Link href="/" className="flex items-center gap-3" aria-label="Sales Forecasting home">
            <span className="flex h-9 w-9 items-center justify-center rounded-lg border border-sky-400/30 bg-sky-400/10 text-sky-300">
              <LineChart aria-hidden="true" className="h-5 w-5" />
            </span>
            <span>
              <span className="block text-sm font-semibold tracking-tight">Sales Forecasting</span>
              <span className="block text-xs text-muted-foreground">MLOps Platform</span>
            </span>
          </Link>

          <nav className="flex items-center gap-2">
            <Link href="/login" className={buttonVariants({ variant: 'ghost', size: 'sm' })}>
              Sign in
            </Link>
            <Link href="/register" className={buttonVariants({ size: 'sm' })}>
              Sign up
            </Link>
          </nav>
        </header>

        <section className="flex flex-1 items-center py-12 sm:py-16 lg:py-20">
          <div className="w-full">
            <div className="max-w-3xl">
              <p className="mb-5 inline-flex items-center gap-2 rounded-full border border-sky-400/20 bg-sky-400/10 px-3 py-1 text-xs font-medium text-sky-200">
                <Sparkles aria-hidden="true" className="h-3.5 w-3.5" />
                Forecasting, scenario planning, and model operations
              </p>
              <h1 className="max-w-3xl text-4xl font-semibold tracking-tight sm:text-5xl lg:text-6xl">
                Make the next sales decision a better-informed one.
              </h1>
              <p className="mt-6 max-w-2xl text-base leading-7 text-muted-foreground sm:text-lg">
                Sales Forecasting brings trusted forecasts, what-if planning, and model monitoring into one focused workspace for retail teams.
              </p>

              <div className="mt-8 flex flex-col gap-3 sm:flex-row">
                <Link href="/register" className={buttonVariants({ size: 'lg' })}>
                  Create your account
                  <ArrowRight aria-hidden="true" className="h-4 w-4" />
                </Link>
                <a
                  href="#capabilities"
                  className={cn(buttonVariants({ variant: 'outline', size: 'lg' }), 'scroll-smooth')}
                >
                  Explore capabilities
                </a>
              </div>

              <p className="mt-4 text-xs text-muted-foreground">
                New here? Sign up first. Already have an account?{' '}
                <Link href="/login" className="font-medium underline underline-offset-4">
                  Sign in
                </Link>
                .
              </p>
            </div>

            <div id="capabilities" className="mt-14 grid gap-4 md:grid-cols-3">
              {capabilities.map(({ title, description, Icon }) => (
                <article key={title} className="rounded-lg border bg-card/70 p-5 shadow-sm backdrop-blur">
                  <span className="flex h-9 w-9 items-center justify-center rounded-md bg-secondary text-sky-300">
                    <Icon aria-hidden="true" className="h-4 w-4" />
                  </span>
                  <h2 className="mt-5 text-base font-semibold">{title}</h2>
                  <p className="mt-2 text-sm leading-6 text-muted-foreground">{description}</p>
                </article>
              ))}
            </div>
          </div>
        </section>

        <footer className="flex flex-col gap-1 border-t py-5 text-xs text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
          <span>Sales Forecasting Platform</span>
          <span>Forecasts are model estimates, not commitments.</span>
        </footer>
      </div>
    </main>
  );
}
