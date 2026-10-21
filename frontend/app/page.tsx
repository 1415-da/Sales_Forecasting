import { redirect } from 'next/navigation';

/**
 * The root sends users to the Forecast Console.
 *
 * A redirect rather than a landing page: with two destinations in the sidebar,
 * a hub page would be a click between the user and the thing they came for.
 */
export default function HomePage() {
  redirect('/forecast');
}
