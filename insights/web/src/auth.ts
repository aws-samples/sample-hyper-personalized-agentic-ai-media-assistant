import {
  AuthenticationDetails,
  CognitoUser,
  CognitoUserPool,
  CognitoUserSession,
} from 'amazon-cognito-identity-js';

export interface AppConfig {
  region: string;
  apiUrl: string;
  userPoolId: string;
  clientId: string;
  demoUrl?: string;
}

let config: AppConfig | null = null;
let pool: CognitoUserPool | null = null;

/** config.json is written by the stack at deploy time (no build-time secrets). */
export async function loadConfig(): Promise<AppConfig> {
  if (config) return config;
  const res = await fetch('/config.json', { cache: 'no-store' });
  if (!res.ok) throw new Error('Missing config.json');
  config = (await res.json()) as AppConfig;
  pool = new CognitoUserPool({ UserPoolId: config.userPoolId, ClientId: config.clientId });
  return config;
}

function sessionOf(user: CognitoUser): Promise<CognitoUserSession | null> {
  return new Promise((resolve) => {
    user.getSession((err: Error | null, s: CognitoUserSession | null) => resolve(err || !s?.isValid() ? null : s));
  });
}

/** The signed-in username, or null. Refreshes an expired session when possible. */
export async function currentUser(): Promise<string | null> {
  const user = pool?.getCurrentUser();
  if (!user) return null;
  return (await sessionOf(user)) ? user.getUsername() : null;
}

export async function idToken(): Promise<string | null> {
  const user = pool?.getCurrentUser();
  if (!user) return null;
  return (await sessionOf(user))?.getIdToken().getJwtToken() ?? null;
}

export function signIn(username: string, password: string): Promise<void> {
  if (!pool) return Promise.reject(new Error('Not configured'));
  const user = new CognitoUser({ Username: username, Pool: pool });
  return new Promise((resolve, reject) => {
    user.authenticateUser(new AuthenticationDetails({ Username: username, Password: password }), {
      onSuccess: () => resolve(),
      onFailure: (err: Error) => reject(err),
      newPasswordRequired: () => reject(new Error('This account needs a new password. Sign in to the demo first.')),
    });
  });
}

export function signOut() {
  pool?.getCurrentUser()?.signOut();
}
