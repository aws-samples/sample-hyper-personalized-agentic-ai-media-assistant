import {
  CognitoUserPool,
  CognitoUser,
  AuthenticationDetails,
  type CognitoUserSession,
} from 'amazon-cognito-identity-js';
import { env } from './env';

/**
 * Thin wrapper around amazon-cognito-identity-js for the demo's Cognito pool.
 * Uses SRP auth (USER_SRP_AUTH). Tokens are persisted to localStorage by the
 * library itself, so `getCurrentUsername` survives reloads.
 */

const pool = new CognitoUserPool({
  UserPoolId: env.cognito.userPoolId,
  ClientId: env.cognito.userPoolClientId,
});

export interface AuthTokens {
  idToken: string;
  accessToken: string;
}

/** Sign in with a username (or email alias) + password. */
export function signIn(username: string, password: string): Promise<AuthTokens> {
  const user = new CognitoUser({ Username: username, Pool: pool });
  const details = new AuthenticationDetails({
    Username: username,
    Password: password,
  });

  return new Promise((resolve, reject) => {
    user.authenticateUser(details, {
      onSuccess: (session: CognitoUserSession) => {
        resolve({
          idToken: session.getIdToken().getJwtToken(),
          accessToken: session.getAccessToken().getJwtToken(),
        });
      },
      onFailure: (err) => reject(err),
      newPasswordRequired: () => {
        // The demo user has a permanent password, so this shouldn't fire.
        reject(new Error('A new password is required for this account.'));
      },
    });
  });
}

/** Username of the currently signed-in user, if any (from persisted session). */
export function getCurrentUsername(): string | null {
  return pool.getCurrentUser()?.getUsername() ?? null;
}

/** Current valid ID token (JWT) for API Gateway's Cognito authorizer, or null. */
export function getIdToken(): Promise<string | null> {
  const user = pool.getCurrentUser();
  if (!user) return Promise.resolve(null);
  return new Promise((resolve) => {
    user.getSession((err: Error | null, session: CognitoUserSession | null) => {
      resolve(
        !err && session && session.isValid()
          ? session.getIdToken().getJwtToken()
          : null,
      );
    });
  });
}

/**
 * Current valid ACCESS token (JWT), or null. AgentCore's inbound authorizer
 * validates the access token's `client_id` against its allowedClients, so the
 * agent invoke path uses this (not the ID token).
 */
export function getAccessToken(): Promise<string | null> {
  const user = pool.getCurrentUser();
  if (!user) return Promise.resolve(null);
  return new Promise((resolve) => {
    user.getSession((err: Error | null, session: CognitoUserSession | null) => {
      resolve(
        !err && session && session.isValid()
          ? session.getAccessToken().getJwtToken()
          : null,
      );
    });
  });
}

/**
 * Resolve to true if there is a valid, non-expired session in storage.
 * Used on app load to restore auth state.
 */
export function hasValidSession(): Promise<boolean> {
  const user = pool.getCurrentUser();
  if (!user) return Promise.resolve(false);
  return new Promise((resolve) => {
    user.getSession((err: Error | null, session: CognitoUserSession | null) => {
      resolve(!err && !!session && session.isValid());
    });
  });
}

/**
 * Cognito groups of the signed-in user, from the ID token's `cognito:groups`
 * claim (empty when signed out or in no group). A user added to a group sees
 * it after their next sign-in, when a new token is issued.
 */
export function getUserGroups(): Promise<string[]> {
  const user = pool.getCurrentUser();
  if (!user) return Promise.resolve([]);
  return new Promise((resolve) => {
    user.getSession((err: Error | null, session: CognitoUserSession | null) => {
      if (err || !session || !session.isValid()) return resolve([]);
      const groups = session.getIdToken().decodePayload()['cognito:groups'];
      resolve(Array.isArray(groups) ? groups.map(String) : []);
    });
  });
}

export function signOut(): void {
  pool.getCurrentUser()?.signOut();
}
