import { useEffect, useState } from 'react';
import { getUserGroups } from './cognito';

/**
 * Booth (kiosk) mode: shared demo logins at an event or on a public screen.
 *
 * A user in the Cognito group `booth` (created by the Application stack, empty
 * by default) gets a read-only household and settings, and their viewing does
 * not change the personas' long-term taste. The backend enforces this
 * (household, profile_settings and profile_events Lambdas read the same
 * `cognito:groups` claim); the UI only hides the controls the API would refuse.
 * See "Shared demo logins (booth mode)" in the README.
 */
export const BOOTH_GROUP = 'booth';

/** True when the signed-in user is in the booth group (false until resolved). */
export function useBoothMode(): boolean {
  const [booth, setBooth] = useState(false);
  useEffect(() => {
    let live = true;
    getUserGroups().then((groups) => live && setBooth(groups.includes(BOOTH_GROUP)));
    return () => {
      live = false;
    };
  }, []);
  return booth;
}
