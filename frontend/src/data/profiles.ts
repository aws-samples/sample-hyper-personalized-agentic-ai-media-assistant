import type { Profile } from './types';
import type { HouseholdMember } from '../lib/api';

/**
 * Profile cards. Household members come from GET /household (see
 * lib/household.tsx); this file only adapts them.
 */

export interface DemoProfile extends Profile {
  /** Key sent to the backend: the member's opaque profile_id. */
  backendProfile: string;
  /**
   * Persona-keyed features that are not data-driven yet (VOD highlights). Absent for members with no matching seed persona.
   */
  personaId?: 'p-movie' | 'p-romcom' | 'p-sports';
  personaSlug?: string;
}

/** Type-only stand-in for screens rendered before the household has loaded
 *  (RequireProfile normally guarantees a real profile). Follows no one. */
export const NO_PROFILE: DemoProfile = {
  id: '', name: '', persona: '', hue: 210, icon: '🙂', backendProfile: '',
};

/** Seed member key -> the persona features that member inherits. */
const PERSONA_BY_MEMBER_KEY: Record<string, Pick<DemoProfile, 'personaId' | 'personaSlug'>> = {
  'action-fan': { personaId: 'p-movie', personaSlug: 'action_fan' },
  'rom-com-lover': { personaId: 'p-romcom', personaSlug: 'rom-com_lover' },
  'sports-fan': { personaId: 'p-sports', personaSlug: 'sports_fan' },
};

export function profileFromMember(m: HouseholdMember): DemoProfile {
  return {
    id: m.profileId,
    name: m.displayName,
    persona: m.displayName,
    hue: m.avatar?.hue ?? 210,
    icon: m.avatar?.icon ?? '🙂',
    kid: Boolean(m.avatar?.kids),
    backendProfile: m.profileId,
    ...(m.memberKey ? PERSONA_BY_MEMBER_KEY[m.memberKey] : undefined),
  };
}
