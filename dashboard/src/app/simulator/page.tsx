import { redirect } from 'next/navigation';

/** The old address of the browser watch: the watch is at /watch, the controls at /presenter. */
export default function OldSimulatorPage() {
  redirect('/watch');
}
